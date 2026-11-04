"""Cursor SDK sidecar for the Hermes `cursor-subscription-directsdk` provider.

Runs inside the plugin's private venv (which has ``cursor-sdk``) and speaks JSON lines on
stdin/stdout. One worker serves every Hermes conversation in the parent process.

Hermes owns the agent loop and every tool. The Cursor agent is created with no built-in tools
(``tools=["mcp"]`` keeps only the MCP group, which is where SDK custom tools live), and each
Hermes tool is registered as a custom tool whose ``execute`` callback *parks*: it records the
call, tells Hermes "the model wants these tool calls", and blocks until Hermes sends the
results back on its next request. The Cursor run then continues in place, so the model keeps
its native multi-step context and Cursor's prompt cache stays warm across tool rounds.

Protocol (one JSON object per line):

  -> {"op": "hello", "id": 1}
  <- {"id": 1, "type": "result", "sdk_version": "..."}

  -> {"op": "models", "id": 2, "api_key": "..."}
  <- {"id": 2, "type": "result", "models": [...]}

  -> {"op": "turn", "id": 3, "session": "s1",
      "create": {"model": "...", "params": [[k, v]], "tools": [{"name", "description", "schema"}],
                 "api_key": "..."} | null,
      "send": {"text": "...", "images": [{"data", "mime_type"}]} | null,
      "results": {"<call id>": {"content": "...", "is_error": false}} | null}
  <- {"id": 3, "type": "text" | "thinking", "text": "..."}            (zero or more)
  <- {"id": 3, "type": "tool_calls", "calls": [{"id", "name", "args"}]}  (segment end, run parked)
  <- {"id": 3, "type": "done", "status": "...", "usage": {...}}         (segment end, run finished)
  <- {"id": 3, "type": "error", "message": "...", "status": 429, "retryable": true}

  -> {"op": "cancel", "session": "s1"}   cancel the run and forget the session
  -> {"op": "close", "session": "s1"}    same, used for idle cleanup
  -> {"op": "shutdown"}
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
import uuid

import cursor_sdk
from cursor_sdk import (
    AgentOptions,
    CursorClient,
    CustomTool,
    LocalAgentOptions,
    ModelParameterValue,
    ModelSelection,
    SDKImage,
    SendOptions,
    UserMessage,
)

CUSTOM_PROVIDER = "custom-user-tools"
# How long a parked tool call waits for Hermes. Hermes tools can run for a long time and approval
# prompts wait on a human, so this is generous; a cancelled or abandoned turn releases it sooner.
TOOL_RESULT_TIMEOUT_S = float(os.environ.get("CURSOR_SUBSCRIPTION_TOOL_TIMEOUT", "7200"))
# Parallel tool calls arrive as separate callbacks a few ms apart. Publish the batch once every
# announced call has called back, or after this grace if an announcement never matches a callback.
BATCH_GRACE_S = 1.0

_out_lock = threading.Lock()


def emit(obj):
    line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def log(*parts):
    sys.stderr.write("[cursor-sidecar] " + " ".join(str(p) for p in parts) + "\n")
    sys.stderr.flush()


def require_key(msg):
    """The API key Hermes resolved for this request. The sidecar has no other credential source."""
    key = msg.get("api_key") if isinstance(msg, dict) else None
    if not isinstance(key, str) or not key.strip():
        raise cursor_sdk.AuthenticationError(
            "Not signed in to Cursor: Hermes has no credential for this provider. Run "
            "`hermes auth add cursor-subscription-directsdk` or set CURSOR_API_KEY.")
    return key.strip()


def error_payload(exc):
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    name = type(exc).__name__
    if status is None:
        status = {"AuthenticationError": 401, "PermissionDeniedError": 403, "RateLimitError": 429,
                  "ConfigurationError": 400, "BadRequestError": 400, "NotFoundError": 404,
                  "InternalServerError": 500, "NetworkError": 503, "APITimeoutError": 504}.get(name)
    return {"type": "error", "message": f"{name}: {exc}", "status": status, "code": getattr(exc, "code", None),
            "retryable": bool(getattr(exc, "is_retryable", False))}


class Pending:
    __slots__ = ("id", "name", "args", "event", "content", "is_error", "published")

    def __init__(self, call_id, name, args):
        self.id, self.name, self.args = call_id, name, args
        self.event = threading.Event()
        self.content, self.is_error, self.published = None, False, False


class Session:
    def __init__(self, worker, sid):
        self.worker, self.sid = worker, sid
        self.lock = threading.RLock()
        self.agent = None
        self.run = None
        self.run_thread = None
        self.sink = None          # request id receiving events, None while parked
        self.buffer = []          # events produced while no request was listening
        self.pending = {}         # call id -> Pending (parked callbacks)
        self.answered = {}        # call id -> Pending already answered (for re-issued calls)
        self.announced = set()    # custom-tool call ids seen in tool-call-started, not yet called back
        self.grace_timer = None
        self.usage = None
        self.closed = False

    # ── event routing ────────────────────────────────────────────────────────────────────────
    def send_event(self, event):
        with self.lock:
            if self.sink is None:
                self.buffer.append(event)
                return
            emit({"id": self.sink, **event})
            if event["type"] in ("tool_calls", "done", "error"):
                self.sink = None

    def attach(self, req_id):
        """Point the event stream at a new Hermes request and flush anything produced meanwhile."""
        with self.lock:
            self.sink = req_id
            buffered, self.buffer = self.buffer, []
        for event in buffered:
            self.send_event(event)

    # ── custom tools ─────────────────────────────────────────────────────────────────────────
    def make_tool(self, name, description, schema):
        def execute(args, ctx):
            call_id = (getattr(ctx, "tool_call_id", None) or f"call_{uuid.uuid4().hex[:24]}")
            with self.lock:
                if self.closed:
                    raise RuntimeError("Request cancelled by Hermes")
                pending = self.pending.get(call_id) or self.answered.get(call_id)
                if pending is None:
                    pending = Pending(call_id, name, dict(args or {}))
                    self.pending[call_id] = pending
                    self.announced.discard(call_id)
                    self._maybe_publish()
                # else: Cursor re-issued a call it already made. A parked callback that blocks for
                # ~270 s makes the agent retry the step with the SAME call id; both callbacks wait
                # on (or reuse) the one result Hermes produces, so the tool runs once.
            if not pending.event.wait(TOOL_RESULT_TIMEOUT_S):
                with self.lock:
                    self.pending.pop(call_id, None)
                raise RuntimeError("Hermes did not return a result for this tool call in time")
            with self.lock:
                self.pending.pop(call_id, None)
                if pending.content is not None:
                    self.answered[call_id] = pending
            if self.closed and pending.content is None:
                raise RuntimeError("Request cancelled by Hermes")
            text = pending.content if isinstance(pending.content, str) else json.dumps(pending.content)
            result = {"content": [{"type": "text", "text": text or "(no output)"}]}
            if pending.is_error:
                result["isError"] = True
            return result

        return CustomTool(execute=execute, description=description or name,
                          input_schema=schema or {"type": "object", "properties": {}})

    def _maybe_publish(self):
        unpublished = [p for p in self.pending.values() if not p.published]
        if not unpublished:
            return
        if self.announced:
            if self.grace_timer is None:
                self.grace_timer = threading.Timer(BATCH_GRACE_S, self._publish_now)
                self.grace_timer.daemon = True
                self.grace_timer.start()
            return
        self._publish_now()

    def _publish_now(self):
        with self.lock:
            if self.grace_timer is not None:
                self.grace_timer.cancel()
                self.grace_timer = None
            batch = [p for p in self.pending.values() if not p.published]
            if not batch or self.closed:
                return
            for p in batch:
                p.published = True
            self.announced.clear()
            self.send_event({"type": "tool_calls",
                             "calls": [{"id": p.id, "name": p.name, "args": p.args} for p in batch]})

    def deliver(self, results):
        missing = []
        with self.lock:
            for call_id, value in (results or {}).items():
                pending = self.pending.get(call_id)
                if pending is None:
                    missing.append(call_id)
                    continue
                pending.content = value.get("content", "")
                pending.is_error = bool(value.get("is_error"))
                pending.event.set()
        return missing

    # ── interaction updates ──────────────────────────────────────────────────────────────────
    def on_delta(self, update):
        kind = getattr(update, "type", None)
        if kind == "text-delta":
            if update.text:
                self.send_event({"type": "text", "text": update.text})
        elif kind == "thinking-delta":
            if update.text:
                self.send_event({"type": "thinking", "text": update.text})
        elif kind == "tool-call-started":
            call = update.tool_call if isinstance(update.tool_call, dict) else {}
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            if call.get("type") == "mcp" and args.get("providerIdentifier") == CUSTOM_PROVIDER:
                with self.lock:
                    if update.call_id not in self.pending and update.call_id not in self.answered:
                        self.announced.add(update.call_id)
        elif kind == "turn-ended":
            usage = getattr(update, "usage", None)
            if isinstance(usage, dict):
                with self.lock:
                    self.usage = usage

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────
    def create(self, spec):
        tools = {t["name"]: self.make_tool(t["name"], t.get("description"), t.get("schema"))
                 for t in spec.get("tools") or []}
        params = [ModelParameterValue(id=str(k), value=str(v)) for k, v in spec.get("params") or []]
        model = ModelSelection(id=spec["model"], params=params)
        options = AgentOptions(
            model=model,
            api_key=require_key(spec),
            name=spec.get("name") or "Hermes",
            # Only the MCP group: no shell/edit/read built-ins, but custom tools stay offered.
            tools=["mcp"] if tools else [],
            local=LocalAgentOptions(cwd=self.worker.workdir, custom_tools=tools or None),
        )
        self.agent = self.worker.client.agents.create(options)

    def start_run(self, send):
        text = send.get("text") or ""
        images = [SDKImage.data_image(i["data"], i["mime_type"]) for i in send.get("images") or []]
        message = UserMessage(text=text, images=images) if images else text
        with self.lock:
            self.usage = None
        run = self.agent.send(message, SendOptions(on_delta=self.on_delta))
        self.run = run

        def wait():
            try:
                result = run.wait()
                if os.environ.get("CURSOR_SUBSCRIPTION_DEBUG"):
                    log("run finished", self.sid, repr(result)[:600])
                usage = None
                if getattr(result, "usage", None) is not None:
                    u = result.usage
                    usage = {"inputTokens": u.input_tokens, "outputTokens": u.output_tokens,
                             "cacheReadTokens": u.cache_read_tokens, "cacheWriteTokens": u.cache_write_tokens,
                             "reasoningTokens": u.reasoning_tokens}
                with self.lock:
                    usage = usage or self.usage
                status = str(getattr(result, "status", "") or "")
                if status in ("error", "expired"):
                    code = getattr(result, "error_code", None) or getattr(result, "error", None)
                    self.send_event({"type": "error", "message": f"Cursor run {status}" + (f": {code}" if code else ""),
                                     "status": None, "retryable": status == "expired"})
                elif status == "cancelled":
                    self.send_event({"type": "error", "message": "Cursor run cancelled", "status": None,
                                     "retryable": False, "code": "cancelled"})
                else:
                    self.send_event({"type": "done", "status": status, "usage": usage,
                                     "model": getattr(getattr(result, "model", None), "id", None)})
            except Exception as exc:  # noqa: BLE001 - every failure must reach Hermes as an event
                log("run failed:", repr(exc))
                self.send_event(error_payload(exc))

        self.run_thread = threading.Thread(target=wait, daemon=True, name=f"cursor-run-{self.sid}")
        self.run_thread.start()

    def close(self):
        if os.environ.get("CURSOR_SUBSCRIPTION_DEBUG"):
            log("close session", self.sid, "".join(traceback.format_stack(limit=6)))
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.grace_timer is not None:
                self.grace_timer.cancel()
            pendings = list(self.pending.values())
        for pending in pendings:
            pending.event.set()
        run = self.run
        if run is not None:
            try:
                if getattr(run, "status", "") == "running":
                    run.cancel()
            except Exception:  # noqa: BLE001
                pass
        agent = self.agent
        if agent is not None:
            try:
                agent.close()
            except Exception:  # noqa: BLE001
                pass
            # Hermes keeps the canonical transcript; a closed Cursor agent is never resumed (a new
            # Hermes process replays instead), so drop Cursor's local copy rather than let them pile up.
            if not os.environ.get("CURSOR_SUBSCRIPTION_KEEP_AGENTS"):
                try:
                    self.worker.client.agents.delete(agent.agent_id, {"cwd": self.worker.workdir})
                except Exception as exc:  # noqa: BLE001
                    log("agent delete failed:", repr(exc)[:200])


class Worker:
    def __init__(self):
        self.workdir = os.environ.get("CURSOR_SUBSCRIPTION_WORKDIR") or os.path.join(
            os.path.expanduser("~"), ".hermes", "cache", "cursor-subscription-directsdk", "workspace")
        os.makedirs(self.workdir, mode=0o700, exist_ok=True)
        self._client = None
        self._client_lock = threading.Lock()
        self.sessions = {}
        self.sessions_lock = threading.Lock()

    @property
    def client(self):
        with self._client_lock:
            if self._client is None:
                self._client = CursorClient.launch_bridge(workspace=self.workdir)
            return self._client

    def session(self, sid, create=False):
        with self.sessions_lock:
            session = self.sessions.get(sid)
            if session is None and create:
                session = self.sessions[sid] = Session(self, sid)
            return session

    def drop(self, sid):
        with self.sessions_lock:
            session = self.sessions.pop(sid, None)
        if session is not None:
            session.close()

    # ── ops ──────────────────────────────────────────────────────────────────────────────────
    def op_hello(self, msg):
        emit({"id": msg.get("id"), "type": "result", "sdk_version": getattr(cursor_sdk, "__version__", None),
              "pid": os.getpid()})

    def op_models(self, msg):
        rows = []
        for m in self.client.models.list(api_key=require_key(msg)):
            rows.append({
                "id": m.id, "display_name": m.display_name, "description": m.description,
                "parameters": [{"id": p.id, "values": [v.value for v in p.values]} for p in m.parameters],
                "default_params": next(([[pv.id, pv.value] for pv in v.params] for v in m.variants if v.is_default), []),
            })
        emit({"id": msg.get("id"), "type": "result", "models": rows})

    def op_turn(self, msg):
        req, sid = msg["id"], msg["session"]
        spec, send, results = msg.get("create"), msg.get("send"), msg.get("results")
        if spec is not None:
            self.drop(sid)
        session = self.session(sid, create=spec is not None)
        if session is None:
            emit({"id": req, "type": "error", "message": "unknown session", "code": "unknown_session",
                  "status": None, "retryable": False})
            return
        try:
            if spec is not None:
                session.create(spec)
            session.attach(req)
            if results:
                missing = session.deliver(results)
                if missing:
                    log("results for unknown calls:", missing)
            if send is not None:
                session.start_run(send)
        except Exception as exc:  # noqa: BLE001
            log("turn failed:", repr(exc))
            if spec is not None:
                self.drop(sid)
            with session.lock:
                session.sink = None
            emit({"id": req, **error_payload(exc)})

    def op_cancel(self, msg):
        self.drop(msg.get("session"))

    op_close = op_cancel

    def serve(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                log("bad protocol line (%d chars)" % len(line))
                continue
            op = msg.get("op")
            if op == "shutdown":
                break
            handler = getattr(self, f"op_{op}", None)
            if handler is None:
                emit({"id": msg.get("id"), "type": "error", "message": f"unknown op {op!r}", "status": None})
                continue

            def run(handler=handler, msg=msg):
                try:
                    handler(msg)
                except Exception as exc:  # noqa: BLE001
                    log(traceback.format_exc())
                    emit({"id": msg.get("id"), **error_payload(exc)})

            threading.Thread(target=run, daemon=True).start()
        self.shutdown()

    def shutdown(self):
        with self.sessions_lock:
            sids = list(self.sessions)
        for sid in sids:
            self.drop(sid)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass


def _isolate_stdio():
    """Move the protocol off fd 0/1 before the SDK spawns its node bridge.

    ``CursorClient.launch_bridge`` starts the bridge with the worker's stdin, and the bridge (or
    a library inside it) reads from it, swallowing protocol lines. Keep private duplicates for the
    protocol and leave fd 0 on a pipe that nobody writes to: never EOF while this worker lives, EOF
    the moment it dies. fd 1 goes to stderr so a stray print can never corrupt the protocol.
    """
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8", buffering=1)
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    read_end, write_end = os.pipe()
    os.dup2(read_end, 0)
    os.close(read_end)
    _keep_open.append(write_end)
    os.dup2(2, 1)
    sys.stdin, sys.stdout = proto_in, proto_out


_keep_open = []


if __name__ == "__main__":
    _isolate_stdio()
    Worker().serve()
