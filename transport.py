"""OpenAI-shaped client that drives Cursor agents through the plugin's SDK sidecar.

Hermes calls ``client.chat.completions.create(messages=..., tools=..., stream=...)`` once per model
step and expects one assistant message back (text and/or tool calls). A Cursor agent instead runs a
whole tool loop inside one *run*. The bridge between the two:

* Each Hermes conversation maps to one Cursor agent (keyed by its system prompt and first message).
* Hermes' tools are registered as Cursor custom tools. When the model calls one, the sidecar parks
  the callback and this client returns ``finish_reason="tool_calls"`` to Hermes.
* Hermes runs the tools (approvals, hooks and all) and calls ``create`` again with the results. If
  the history is exactly what this client last saw plus those results, the results are handed to
  the parked callbacks and the same Cursor run continues ("resume").
* A new user message after a finished run continues the same agent ("follow-up").
* Anything else (compression, edits, retries, a restart, a model or toolset change) starts a new
  Cursor agent seeded with Hermes' system prompt and a transcript of the history ("replay").
"""
from __future__ import annotations

import atexit
import copy
import hashlib
import itertools
import json
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import OrderedDict, deque
from types import SimpleNamespace

try:
    from . import cursor_catalog as catalog
    from . import cursor_runtime as rt
except ImportError:
    import cursor_catalog as catalog
    import cursor_runtime as rt

logger = logging.getLogger(__name__)

BASE_URL = rt.BASE_URL
DEFAULT_EVENT_TIMEOUT_S = float(os.environ.get("CURSOR_SUBSCRIPTION_EVENT_TIMEOUT", "900"))
IDLE_SESSION_S = 45 * 60
MAX_SESSIONS = 32


# ── errors Hermes can classify (status_code drives retry vs fallback) ──────────────────────────

class CursorError(RuntimeError):
    def __init__(self, message, status_code=None, retryable=False, code=None):
        super().__init__(message)
        self.status_code, self.retryable, self.code = status_code, retryable, code


class CursorRuntimeMissing(CursorError):
    def __init__(self, message=rt.INSTALL_HINT):
        super().__init__(message, 503)


class CursorLoggedOut(CursorError):
    def __init__(self, message=rt.LOGGED_OUT_HINT):
        super().__init__(message, 401)


# ── response objects ────────────────────────────────────────────────────────────────────────────

class Object(SimpleNamespace):
    def model_dump(self, **_):
        def unpack(value):
            if isinstance(value, Object):
                return {k: unpack(v) for k, v in vars(value).items()}
            if isinstance(value, list):
                return [unpack(v) for v in value]
            return copy.deepcopy(value)
        return unpack(self)

    def get(self, key, default=None):
        return getattr(self, key, default)


def obj(value):
    if isinstance(value, dict):
        return Object(**{k: obj(v) for k, v in value.items()})
    if isinstance(value, list):
        return [obj(v) for v in value]
    return value


def _chunk(model, delta, finish=None, usage=None):
    base = {"content": None, "tool_calls": None, "reasoning_content": None}
    return obj({"id": "cursor-native", "object": "chat.completion.chunk", "model": model, "created": int(time.time()),
                "choices": [{"index": 0, "delta": {**base, **delta}, "finish_reason": finish}], "usage": usage})


# ── sidecar process (one per Hermes process) ───────────────────────────────────────────────────

class Sidecar:
    def __init__(self, command):
        self.command = list(command)
        self.proc = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.queues = {}
        self.ids = itertools.count(1)
        self.stderr = deque(maxlen=60)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def ensure(self):
        with self.lock:
            if self.alive():
                return
            if not rt.runtime_present():
                raise CursorRuntimeMissing()
            env = rt.clean_env({"CURSOR_SUBSCRIPTION_WORKDIR": str(rt.state_dir() / "workspace")})
            self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                                         env=env, start_new_session=True)
            proc = self.proc
            threading.Thread(target=self._read, args=(proc,), daemon=True, name="cursor-sidecar-out").start()
            threading.Thread(target=self._drain, args=(proc,), daemon=True, name="cursor-sidecar-err").start()

    def _read(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    logger.debug("cursor sidecar: non-JSON stdout %r", line[:200])
                    continue
                q = self.queues.get(msg.get("id"))
                if q is not None:
                    q.put(msg)
        finally:
            proc.wait()
            tail = " ".join(s.strip() for s in list(self.stderr)[-8:] if s.strip())
            dead = {"type": "error", "message": f"Cursor sidecar exited (code {proc.returncode})"
                    + (f": {tail[:500]}" if tail else ""), "status": 503, "retryable": True, "code": "sidecar_exit"}
            if proc.returncode == 3:
                dead.update(message=rt.INSTALL_HINT, retryable=False)
            for q in list(self.queues.values()):
                q.put(dead)

    def _drain(self, proc):
        for line in proc.stderr:
            self.stderr.append(line)
            logger.debug("cursor sidecar: %s", line.rstrip())

    def send(self, payload):
        self.ensure()
        with self.write_lock:
            try:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise CursorError(f"Cursor sidecar is not accepting requests: {exc}", 503, True) from exc

    def open(self, op, **fields):
        req = next(self.ids)
        q = queue.Queue()
        self.queues[req] = q
        try:
            self.send({"op": op, "id": req, **fields})
        except BaseException:
            self.queues.pop(req, None)
            raise
        return req, q

    def finish(self, req):
        self.queues.pop(req, None)

    def call(self, op, timeout=60, **fields):
        req, q = self.open(op, **fields)
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty as exc:
            raise CursorError(f"Cursor sidecar timed out on {op}", 504, True) from exc
        finally:
            self.finish(req)
        if msg.get("type") == "error":
            raise _error_from(msg)
        return msg

    def notify(self, op, **fields):
        if self.alive():
            try:
                self.send({"op": op, **fields})
            except CursorError:
                pass

    def stop(self):
        if self.alive():
            try:
                self.send({"op": "shutdown"})
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


_sidecar = None
_sidecar_lock = threading.Lock()


def get_sidecar(command=None):
    global _sidecar
    with _sidecar_lock:
        wanted = list(command) if command else [str(rt.LAUNCHER)]
        if _sidecar is None or (_sidecar.command != wanted and not _sidecar.alive()):
            _sidecar = Sidecar(wanted)
        return _sidecar


atexit.register(lambda: _sidecar.stop() if _sidecar is not None else None)


def _error_from(msg):
    status, text = msg.get("status"), msg.get("message") or "Cursor request failed"
    if status == 401 or msg.get("code") == "unauthenticated" or "Not signed in" in text:
        # 401 either way, so Hermes rotates to the next pooled key; only the advice differs.
        hint = rt.LOGGED_OUT_HINT if "Not signed in" in text else rt.REJECTED_HINT
        return CursorLoggedOut(f"{hint} ({text})")
    return CursorError(text, status, bool(msg.get("retryable")), msg.get("code"))


# ── message normalization ──────────────────────────────────────────────────────────────────────

def _text_of(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append(part.get("text") or "")
        elif part.get("type") in ("image_url", "image", "input_image"):
            parts.append("[image]")
    return "\n".join(p for p in parts if p)


def _images_of(content):
    images = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    mime, data = url[5:].split(";base64,", 1)
                    images.append({"data": data, "mime_type": mime})
    return images


def _canon_args(arguments):
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            return arguments.strip()
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(message):
    role = message.get("role")
    body = {"role": role, "content": _text_of(message.get("content")).strip()}
    if role == "assistant":
        body["calls"] = [(tc.get("id"), (tc.get("function") or {}).get("name"),
                          _canon_args((tc.get("function") or {}).get("arguments")))
                         for tc in message.get("tool_calls") or []]
    elif role == "tool":
        body["id"] = message.get("tool_call_id")
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def split_system(messages):
    system, rest = [], []
    for message in messages:
        if message.get("role") in ("system", "developer") and not rest:
            system.append(_text_of(message.get("content")))
        else:
            rest.append(message)
    return "\n\n".join(s for s in system if s), rest


SYSTEM_WRAPPER = (
    "<hermes_operating_instructions>\n"
    "You are the model behind Hermes Agent, running through Cursor. Hermes owns this conversation: "
    "the instructions below are your system prompt and take precedence over any Cursor defaults. "
    "Cursor's built-in tools (shell, file edit, search) are disabled in this session; the only tools "
    "you can call are the ones provided to you, which Hermes executes. Never claim to have run a "
    "command or edited a file unless a provided tool did it.\n\n"
    "{system}\n"
    "</hermes_operating_instructions>"
)


def _render_transcript(messages):
    lines = []
    for message in messages:
        role = message.get("role")
        text = _text_of(message.get("content")).strip()
        if role == "user":
            lines.append(f"[user]\n{text}")
        elif role == "assistant":
            if text:
                lines.append(f"[assistant]\n{text}")
            for tc in message.get("tool_calls") or []:
                fn = tc.get("function") or {}
                lines.append(f"[assistant called tool {fn.get('name')} (id {tc.get('id')})]\n"
                             f"{_canon_args(fn.get('arguments'))}")
        elif role == "tool":
            lines.append(f"[tool result (id {message.get('tool_call_id')})]\n{text}")
    return "\n\n".join(lines)


def build_seed_prompt(system, rest, response_format=None):
    """First message for a brand-new Cursor agent: Hermes' system prompt, then the history."""
    blocks = []
    if system:
        blocks.append(SYSTEM_WRAPPER.format(system=system))
    # The trailing run of user messages is the live request; everything before it is history.
    cut = len(rest)
    while cut > 0 and rest[cut - 1].get("role") == "user":
        cut -= 1
    history, live = rest[:cut], rest[cut:]
    if history:
        blocks.append("<conversation_history>\nThe conversation so far (already happened; tool results "
                      "shown are real):\n\n" + _render_transcript(history) + "\n</conversation_history>")
    if live:
        blocks.append("\n\n".join(_text_of(m.get("content")) for m in live))
    else:
        blocks.append("Continue the conversation from where it left off, using the latest tool results above.")
    if response_format:
        blocks.append(_format_instruction(response_format))
    images = [img for m in live for img in _images_of(m.get("content"))]
    return "\n\n".join(blocks), images


def _format_instruction(response_format):
    if not isinstance(response_format, dict) or response_format.get("type") in (None, "text"):
        return ""
    schema = (response_format.get("json_schema") or {}).get("schema")
    if schema:
        return ("Reply with only a single JSON value that matches this JSON Schema, with no prose and no "
                "code fences:\n" + json.dumps(schema, ensure_ascii=False))
    return "Reply with only a single valid JSON object, with no prose and no code fences."


# ── tool schemas ────────────────────────────────────────────────────────────────────────────────

def normalize_tools(tools):
    rows = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            continue
        fn = tool.get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        schema = fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object"}
        schema = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf", "allOf")}
        schema.setdefault("type", "object")
        if schema.get("type") == "object" and not isinstance(schema.get("properties"), dict):
            schema["properties"] = {}
        rows.append({"name": name, "description": fn.get("description") or "", "schema": schema})
    return rows


# ── conversation registry ──────────────────────────────────────────────────────────────────────

class Conversation:
    def __init__(self, sid, model_key, tools_sig):
        self.sid, self.model_key, self.tools_sig = sid, model_key, tools_sig
        self.consumed = []   # digests of the non-system messages the Cursor agent has seen
        self.pending = []    # tool calls published to Hermes, awaiting results
        self.busy = False
        self.last_used = time.monotonic()


_conversations = OrderedDict()
_conv_lock = threading.Lock()


def _sweep(sidecar):
    now = time.monotonic()
    stale = []
    with _conv_lock:
        for fp, conv in list(_conversations.items()):
            limit = IDLE_SESSION_S * (4 if conv.pending else 1)
            if not conv.busy and now - conv.last_used > limit:
                stale.append(_conversations.pop(fp))
        while len(_conversations) > MAX_SESSIONS:
            fp, conv = next(((f, c) for f, c in _conversations.items() if not c.busy), (None, None))
            if fp is None:
                break
            stale.append(_conversations.pop(fp))
    for conv in stale:
        sidecar.notify("close", session=conv.sid)


def _plan(conv, digests, rest, model_key, tools_sig):
    """("resume", results, extra) | ("followup", text, images) | None (replay into a new agent)."""
    if conv is None or conv.busy or conv.model_key != model_key or conv.tools_sig != tools_sig:
        return None
    n = len(conv.consumed)
    if digests[:n] != conv.consumed:
        return None
    tail = rest[n:]
    if conv.pending:
        ids = [call["id"] for call in conv.pending]
        results, i = {}, 0
        while i < len(tail) and tail[i].get("role") == "tool":
            message = tail[i]
            results[message.get("tool_call_id")] = {"content": _text_of(message.get("content")),
                                                    "is_error": bool(message.get("is_error"))}
            i += 1
        after = tail[i:]
        if not set(ids) <= set(results) or any(m.get("role") != "user" for m in after):
            return None
        extra = "\n\n".join(_text_of(m.get("content")) for m in after).strip()
        if extra:
            last = ids[-1]
            results[last]["content"] += f"\n\n[Message from the user while this tool ran]\n{extra}"
        return "resume", {k: v for k, v in results.items() if k in ids}, None
    if tail and all(m.get("role") == "user" for m in tail):
        text = "\n\n".join(_text_of(m.get("content")) for m in tail)
        images = [img for m in tail for img in _images_of(m.get("content"))]
        return "followup", text, images
    return None


# ── the client Hermes talks to ─────────────────────────────────────────────────────────────────

class Client:
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(self, api_key=None, timeout=None, **_):
        # The key Hermes resolved for this provider (credential pool or CURSOR_API_KEY); handed to
        # the sidecar with each request, never logged or written anywhere.
        self.api_key = api_key if isinstance(api_key, str) else ""
        self.base_url = BASE_URL
        self.command = [str(rt.LAUNCHER)]
        self._key_id = hashlib.sha256(self.api_key.encode()).hexdigest()[:16]
        self.timeout = timeout if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_EVENT_TIMEOUT_S
        self._lock = threading.Lock()
        self._active = set()   # session ids with a request in flight from this client
        self._closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    # Hermes' interrupt path: stop every in-flight request of this client, promptly, from any thread.
    def cancel(self):
        with self._lock:
            sids = tuple(self._active)
        sidecar = get_sidecar(self.command)
        for sid in sids:
            sidecar.notify("cancel", session=sid)

    def close(self):
        self._closed = True
        self.cancel()

    def create(self, **kwargs):
        if self._closed:
            raise CursorError("Cursor client is closed")
        if not self.api_key.strip():
            raise CursorLoggedOut()
        stream = self._stream(**kwargs)
        if kwargs.get("stream"):
            return _Stream(stream, self.cancel)
        response = None
        try:
            for chunk in stream:
                if hasattr(chunk, "_response"):
                    response = chunk._response
        finally:
            stream.close()
        if response is None:
            raise CursorError("Cursor returned no response")
        return response

    def _stream(self, **kwargs):
        model = kwargs.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("model is required")
        messages = kwargs.get("messages") or []
        system, rest = split_system(messages)
        if not rest:
            raise ValueError("At least one user message is required")
        tools = normalize_tools(kwargs.get("tools"))
        extra = kwargs.get("extra_body") if isinstance(kwargs.get("extra_body"), dict) else {}
        reasoning = extra.get("reasoning") if isinstance(extra.get("reasoning"), dict) else None
        response_format = kwargs.get("response_format") or extra.get("response_format")
        auxiliary = kwargs.get("hermes_auxiliary") is True
        model_id, params = catalog.resolve_params(model, reasoning)
        model_key = (model_id, tuple(params))
        tools_sig = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
        digests = [digest(m) for m in rest]
        fingerprint = hashlib.sha256(json.dumps([self._key_id, system, digests[0], auxiliary]).encode()).hexdigest()

        sidecar = get_sidecar(self.command)
        _sweep(sidecar)
        with _conv_lock:
            conv = _conversations.get(fingerprint)
            plan = None if response_format else _plan(conv, digests, rest, model_key, tools_sig)
            if plan is None:
                if conv is not None and not conv.busy:
                    sidecar.notify("close", session=conv.sid)
                conv = Conversation(f"h-{uuid.uuid4().hex[:16]}", model_key, tools_sig)
                _conversations[fingerprint] = conv
            conv.busy = True
            conv.last_used = time.monotonic()
            _conversations.move_to_end(fingerprint)

        if plan is None:
            text, images = build_seed_prompt(system, rest, response_format)
            payload = {"create": {"model": model_id, "params": [list(p) for p in params], "tools": tools,
                                  "name": "Hermes" + (" (auxiliary)" if auxiliary else ""),
                                  "api_key": self.api_key},
                       "send": {"text": text, "images": images}, "results": None}
            mode = "replay" if len(rest) > 1 else "new"
        elif plan[0] == "resume":
            payload = {"create": None, "send": None, "results": plan[1]}
            mode = "resume"
        else:
            payload = {"create": None, "send": {"text": plan[1], "images": plan[2]}, "results": None}
            mode = "followup"
        logger.info("%s: %s session=%s model=%s tools=%d msgs=%d",
                     rt.PROVIDER, mode, conv.sid, model_id, len(tools), len(rest))
        return self._events(sidecar, conv, fingerprint, digests, payload, model, auxiliary and not tools)

    def _events(self, sidecar, conv, fingerprint, digests, payload, model, one_shot):
        with self._lock:
            self._active.add(conv.sid)
        req = None
        finished = False
        text_parts, thinking_parts = [], []
        try:
            req, q = sidecar.open("turn", session=conv.sid, **payload)
            while True:
                try:
                    event = q.get(timeout=self.timeout)
                except queue.Empty as exc:
                    raise CursorError(f"Cursor stopped responding for {int(self.timeout)}s", 504, True,
                                      "timeout") from exc
                kind = event.get("type")
                if kind == "text":
                    text_parts.append(event["text"])
                    yield _chunk(model, {"content": event["text"]})
                elif kind == "thinking":
                    thinking_parts.append(event["text"])
                    yield _chunk(model, {"reasoning_content": event["text"]})
                elif kind in ("tool_calls", "done"):
                    calls = []
                    if kind == "tool_calls":
                        for call in event.get("calls") or []:
                            calls.append({"id": call["id"], "type": "function",
                                          "function": {"name": call["name"],
                                                       "arguments": json.dumps(call.get("args") or {},
                                                                               ensure_ascii=False)}})
                    text = "".join(text_parts)
                    thinking = "".join(thinking_parts) or None
                    message = {"role": "assistant", "content": text or None, "tool_calls": calls or None,
                               "reasoning_content": thinking}
                    with _conv_lock:
                        conv.consumed = digests + [digest(message)]
                        conv.pending = [{"id": c["id"]} for c in calls]
                        conv.busy = False
                        conv.last_used = time.monotonic()
                    finished = True
                    finish = "tool_calls" if calls else "stop"
                    native = event.get("usage") if kind == "done" else None
                    response = obj({"id": f"cursor-{conv.sid}", "object": "chat.completion", "model": model,
                                    "created": int(time.time()),
                                    "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                                    # Cursor reports one cumulative usage per run (every step summed), which
                                    # is not this step's prompt size; leave usage unset so Hermes' own context
                                    # estimate drives compression. The raw totals ride on `cursor_usage`.
                                    "usage": None, "cursor_usage": native})
                    delta = {"tool_calls": [dict(c, index=i) for i, c in enumerate(calls)] or None}
                    final = _chunk(model, delta, finish)
                    final._response = response
                    yield final
                    if kind == "done" and one_shot:
                        self._forget(sidecar, fingerprint, conv)
                    return
                elif kind == "error":
                    raise _error_from(event)
        finally:
            if req is not None:
                sidecar.finish(req)
            with self._lock:
                self._active.discard(conv.sid)
            if not finished:
                # Interrupted, timed out or failed mid-segment: the Cursor run cannot be trusted to
                # line up with Hermes' history any more, so cancel it and replay next time.
                self._forget(sidecar, fingerprint, conv, cancel=True)

    @staticmethod
    def _forget(sidecar, fingerprint, conv, cancel=False):
        with _conv_lock:
            if _conversations.get(fingerprint) is conv:
                _conversations.pop(fingerprint, None)
            conv.busy = False
        sidecar.notify("cancel" if cancel else "close", session=conv.sid)


class _Stream:
    """Iterator wrapper with close()/context-manager support, as Hermes' stream consumer expects."""

    def __init__(self, generator, on_close=None):
        self._gen = generator
        self._on_close = on_close
        self._lock = threading.Lock()

    def __iter__(self):
        return self

    def __next__(self):
        with self._lock:
            return next(self._gen)

    def close(self):
        # A consumer blocked in next() on another thread unwinds through the cancel; a paused or
        # unstarted generator is finalized here.
        if self._lock.acquire(blocking=False):
            try:
                self._gen.close()
            finally:
                self._lock.release()
        elif self._on_close is not None:
            self._on_close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def list_models(api_key, timeout=60):
    """Live catalog for the account behind *api_key*; refreshes the on-disk cache. None when unavailable."""
    if not api_key or not rt.runtime_present():
        return None
    try:
        rows = get_sidecar().call("models", timeout=timeout, api_key=api_key).get("models")
    except CursorError as exc:
        logger.debug("%s: model list failed: %s", rt.PROVIDER, exc)
        return None
    if isinstance(rows, list) and rows:
        catalog.store(rows)
        return rows
    return None
