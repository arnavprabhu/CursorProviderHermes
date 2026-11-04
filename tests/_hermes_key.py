"""Cursor key for the live dev scripts, resolved inside the process so it is never exported,
echoed or printed.

Order: ``CURSOR_API_KEY`` if set, else whatever Hermes' own credential pool selects for this
provider (the row ``hermes auth add cursor-subscription-directsdk`` created). The pool route needs
Hermes' interpreter and source tree, e.g.:

    ~/.hermes/hermes-agent/venv/bin/python tests/live_loop.py

``HERMES_AGENT_ROOT`` overrides the source location (default ``~/.hermes/hermes-agent``).
"""
import os
import sys
from pathlib import Path

PROVIDER = "cursor-subscription-directsdk"


def cursor_key():
    key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if key:
        return key
    root = os.environ.get("HERMES_AGENT_ROOT") or str(Path.home() / ".hermes" / "hermes-agent")
    if root not in sys.path:
        sys.path.append(root)
    try:
        from agent.credential_pool import load_pool
        entry = load_pool(PROVIDER).select()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Could not read Hermes' credential pool ({type(exc).__name__}). Run this with "
                         "Hermes' python (~/.hermes/hermes-agent/venv/bin/python) or set CURSOR_API_KEY.")
    if entry is None or not entry.runtime_api_key:
        raise SystemExit(f"No Cursor credential: run `hermes auth add {PROVIDER}` or set CURSOR_API_KEY.")
    return entry.runtime_api_key
