"""Cursor model catalog: cached live list, context windows and reasoning-effort mapping.

The live list comes from the SDK (`Cursor.models.list`, account-specific) and is cached on disk so
hot paths (context length, effort mapping) never spawn anything. Model ids are Cursor's own
(``claude-opus-5-5``, ``gpt-5.5``, ``composer-2.5``), except Auto: Cursor's ``default`` is shown to
Hermes as ``cursor-auto`` (see ``AUTO_ID``). A Cursor model parameter
can be pinned in the id after ``@``: ``claude-opus-5-5@context=1m,fast=true``.
"""
from __future__ import annotations

import json
import re
import time

try:
    from . import cursor_runtime as rt
except ImportError:  # flat import (tests, setup CLI)
    import cursor_runtime as rt

CACHE_TTL_S = 6 * 3600
DEFAULT_CONTEXT = 200_000

# Cursor Auto: Cursor picks the model per request (its catalog id is ``default``). Hermes sees it as
# ``cursor-auto``: a bare ``auto`` is Hermes' own "inherit / auto-detect" sentinel for auxiliary
# models, and ``default`` reads like a config placeholder. ``auto`` and ``default`` still resolve.
AUTO_ID = "cursor-auto"
CURSOR_AUTO_ID = "default"
AUTO_NOTE = "Auto: Cursor picks the model for each request"

# Shown when the live list is unavailable (logged out, runtime missing). Ids verified against the
# live catalog when this plugin was written; the live list always wins when it loads.
FALLBACK_MODELS = (
    AUTO_ID, "composer-2.5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "gpt-5.5",
    "gpt-5.6-sol", "gemini-3.1-pro", "grok-4.7",
)
EFFORT_PARAMS = ("effort", "reasoning_effort", "reasoning")
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_ALIASES = {"extra-high": "xhigh", "extra_high": "xhigh"}

_memo = {"mtime": None, "rows": None}

# Short names; Hermes resolves them via the profile's model_aliases, the client resolves them too.
ALIASES = {
    "auto": AUTO_ID,
    "composer": "composer-2.5",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "gpt": "gpt-5.5",
    "gemini": "gemini-3.1-pro",
    "grok": "grok-4.7",
}


def cache_path():
    return rt.state_dir() / "models.json"


def load_cached(max_age=None):
    path = cache_path()
    try:
        stat = path.stat()
    except OSError:
        return None
    if max_age is not None and time.time() - stat.st_mtime > max_age:
        return None
    if _memo["mtime"] == (str(path), stat.st_mtime) and _memo["rows"] is not None:
        return _memo["rows"]
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("models")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(rows, list):
        return None
    _memo.update(mtime=(str(path), stat.st_mtime), rows=rows)
    return rows


def store(rows):
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "models": rows}), encoding="utf-8")
    tmp.replace(path)


def cursor_id(model_id):
    """Hermes-facing id -> the id Cursor's API expects (``cursor-auto`` -> ``default``)."""
    model_id = ALIASES.get(model_id, model_id)
    return CURSOR_AUTO_ID if model_id == AUTO_ID else model_id


def hermes_id(model_id):
    """Cursor catalog id -> the id Hermes shows and stores (``default`` -> ``cursor-auto``)."""
    return AUTO_ID if model_id == CURSOR_AUTO_ID else model_id


def split_model(model):
    """``id@k=v,k2=v2`` -> (Cursor id, {k: v})."""
    base, _, tail = (model or "").partition("@")
    base = cursor_id(base.strip())
    pins = {}
    for part in filter(None, (p.strip() for p in tail.split(","))):
        key, _, value = part.partition("=")
        if key.strip() and value.strip():
            pins[key.strip()] = value.strip()
    return base, pins


def find(model_id):
    for row in load_cached() or ():
        if row.get("id") == model_id:
            return row
    return None


def _parse_window(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([km]?)\s*", str(value).lower())
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2)
    return int(number * {"k": 1_000, "m": 1_000_000, "": 1}[unit])


def context_length(model):
    model_id, pins = split_model(model)
    row = find(model_id)
    if row is None:
        return None
    defaults = dict(row.get("default_params") or [])
    value = pins.get("context") or defaults.get("context")
    if value is None:
        options = next((p.get("values") for p in row.get("parameters") or [] if p.get("id") == "context"), None)
        value = options[0] if options else None
    return _parse_window(value) if value is not None else DEFAULT_CONTEXT


def _normalize_effort(value):
    value = str(value or "").strip().lower()
    return _ALIASES.get(value, value)


def resolve_params(model, reasoning=None):
    """(cursor model id, [(param, value)]) for a request: pins from the id, then Hermes' reasoning
    setting mapped onto whichever effort/thinking parameter this model exposes (clamped down to
    the nearest value it offers, never escalated)."""
    model_id, pins = split_model(model)
    row = find(model_id)
    # Start from the model's default variant so an override never silently changes the other
    # parameters (the context window reported to Hermes is the default variant's).
    params = dict((row or {}).get("default_params") or []) if (pins or reasoning) else {}
    params.update(pins)
    if row is not None and isinstance(reasoning, dict) and reasoning:
        pinned = set(pins)  # an explicit @pin in the model id beats Hermes' reasoning setting
        offered = {p.get("id"): [str(v) for v in p.get("values") or []] for p in row.get("parameters") or []}
        disabled = reasoning.get("enabled") is False or _normalize_effort(reasoning.get("effort")) == "none"
        if disabled and "thinking" in offered and "false" in offered["thinking"] and "thinking" not in pinned:
            params["thinking"] = "false"
        effort_param = next((name for name in EFFORT_PARAMS if name in offered), None)
        wanted = "none" if disabled else _normalize_effort(reasoning.get("effort"))
        if effort_param and wanted and effort_param not in pinned:
            values = offered[effort_param]
            by_norm = {_normalize_effort(v): v for v in values}
            if wanted in by_norm:
                params[effort_param] = by_norm[wanted]
            elif wanted in _EFFORT_ORDER:
                rank = _EFFORT_ORDER.index(wanted)
                lower = [v for v in by_norm if v in _EFFORT_ORDER and _EFFORT_ORDER.index(v) <= rank]
                if lower:
                    params[effort_param] = by_norm[max(lower, key=_EFFORT_ORDER.index)]
        if (reasoning.get("enabled") is True and not disabled and "thinking" in offered
                and "true" in offered["thinking"] and "thinking" not in pinned):
            params["thinking"] = "true"
    return model_id, sorted(params.items())


def model_ids(rows=None):
    """Hermes-facing ids for the picker, Auto first (Cursor lists it wherever it likes)."""
    rows = rows if rows is not None else load_cached()
    ids = [hermes_id(row["id"]) for row in rows or () if isinstance(row, dict) and row.get("id")]
    return sorted(ids, key=lambda i: i != AUTO_ID)
