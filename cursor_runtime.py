"""Paths, install and login for the Cursor SDK runtime this plugin drives.

The Cursor SDK (``cursor-sdk`` on PyPI, which bundles Cursor's own node bridge) lives in a private
venv under ``<plugin>/.runtime/venv`` so it never touches Hermes' own environment. Login is
Cursor's official SDK browser flow; it stores a revocable user API key in
``~/.cursor/sdk/auth.json`` (0600), which the SDK reads by itself. This module only reports
whether that file exists and when it expires; it never prints or copies the key.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("CURSOR_SUBSCRIPTION_RUNTIME_DIR") or PLUGIN_DIR / ".runtime")
VENV_DIR = RUNTIME_DIR / "venv"
LAUNCHER = PLUGIN_DIR / "bin" / "cursor-sidecar"
WORKER = PLUGIN_DIR / "sidecar" / "worker.py"
AUTH_HELPER = PLUGIN_DIR / "sidecar" / "cursor_auth.mjs"
CATALOG_CACHE = RUNTIME_DIR / "models.json"
# Pinned: the sidecar is qualified against this SDK (custom-tool callbacks, tools=["mcp"]).
SDK_REQUIREMENT = os.environ.get("CURSOR_SUBSCRIPTION_SDK_REQUIREMENT", "cursor-sdk==1.3.1")
PROVIDER = "cursor-subscription"

INSTALL_HINT = (f"The Cursor SDK runtime is not installed. Run `{sys.executable} {PLUGIN_DIR / 'cursor_setup.py'} "
                "install`, or pick Cursor Subscription in `hermes model`, which installs it.")
LOGGED_OUT_HINT = (f"Not signed in to Cursor. Run `{sys.executable} {PLUGIN_DIR / 'cursor_setup.py'} login` "
                   "(or pick Cursor Subscription in `hermes model`), or set CURSOR_API_KEY.")


def venv_python() -> Path:
    override = os.environ.get("CURSOR_SUBSCRIPTION_PYTHON")
    if override:
        return Path(override)
    return VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_installed() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    try:
        out = subprocess.run([str(py), "-I", "-c", "import cursor_sdk, sys; sys.stdout.write('ok')"],
                             capture_output=True, text=True, timeout=30, env=_clean_env())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.stdout == "ok"


def runtime_present() -> bool:
    """Cheap check (no subprocess) for hot paths."""
    return venv_python().exists()


def _clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    env.update(extra or {})
    return env


def _find_uv():
    found = shutil.which("uv")
    if found:
        return found
    for base in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (base / "uv").exists():
            return str(base / "uv")
    tools = Path.home() / ".hermes" / "tools"
    if tools.is_dir():
        for candidate in sorted(tools.glob("uv-*/uv"), reverse=True):
            if os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def install(*, quiet: bool = False) -> None:
    """Create the private venv and install the pinned Cursor SDK (about 200 MB, bundles node)."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    uv = _find_uv()
    say = (lambda *_: None) if quiet else (lambda msg: print(msg, flush=True))
    if not venv_python().exists():
        say(f"Creating Cursor SDK venv in {VENV_DIR} ...")
        if uv:
            subprocess.run([uv, "venv", "--quiet", "--python", sys.executable, str(VENV_DIR)],
                           check=True, env=_clean_env())
        else:
            subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True, env=_clean_env())
    say(f"Installing {SDK_REQUIREMENT} ...")
    if uv:
        subprocess.run([uv, "pip", "install", "--quiet", "--python", str(venv_python()), SDK_REQUIREMENT],
                       check=True, env=_clean_env())
    else:
        subprocess.run([str(venv_python()), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                        SDK_REQUIREMENT], check=True, env=_clean_env())
    if not runtime_installed():
        raise RuntimeError("cursor-sdk did not import after install")
    say("Cursor SDK runtime ready.")


# ── login state ─────────────────────────────────────────────────────────────────────────────────

def auth_file() -> Path:
    return Path(os.path.expanduser(os.environ.get("CURSOR_SDK_AUTH_FILE", "~/.cursor/sdk/auth.json")))


def auth_state() -> dict:
    """``{logged_in, source, email, expires_at_ms}``; never includes the key."""
    if os.environ.get("CURSOR_API_KEY", "").strip():
        return {"logged_in": True, "source": "CURSOR_API_KEY", "email": None, "expires_at_ms": None}
    try:
        data = json.loads(auth_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"logged_in": False, "source": None, "email": None, "expires_at_ms": None}
    if not isinstance(data, dict) or not isinstance(data.get("apiKey"), str) or not data["apiKey"]:
        return {"logged_in": False, "source": None, "email": None, "expires_at_ms": None}
    expires = data.get("apiKeyExpiresAtMs")
    expired = isinstance(expires, (int, float)) and expires < time.time() * 1000
    return {"logged_in": not expired, "source": "cursor-sdk-login", "email": data.get("email"),
            "expires_at_ms": expires, "expired": expired}


def _bridge_dir() -> Path | None:
    py = venv_python()
    try:
        out = subprocess.run([str(py), "-I", "-c", "import cursor_sdk, os; print(os.path.dirname(cursor_sdk.__file__))"],
                             capture_output=True, text=True, timeout=30, env=_clean_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    base = Path(out.stdout.strip()) / "_vendor" / "bridge"
    return base if (base / "bin").is_dir() else None


def run_auth(command: str) -> int:
    """Run Cursor's SDK login/logout/status with the node bundled in the SDK wheel."""
    bridge = _bridge_dir()
    if bridge is None:
        print(INSTALL_HINT, file=sys.stderr)
        return 2
    node = bridge / "bin" / ("node.exe" if os.name == "nt" else "node")
    sdk = (bridge / "node_modules" / "@cursor" / "sdk" / "dist" / "esm" / "index.js").as_uri()
    proc = subprocess.run([str(node), str(AUTH_HELPER), sdk, command], env=_clean_env(), text=True,
                          stdout=subprocess.PIPE)
    try:
        result = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {"ok": False, "error": (proc.stdout or "").strip()[:300]}
    if command == "login":
        if result.get("ok"):
            who = f" as {result['email']}" if result.get("email") else ""
            print(f"Signed in to Cursor{who}. The key is stored in {auth_file()} (revocable in the Cursor dashboard).")
        else:
            print(f"Cursor login failed: {result.get('error')}", file=sys.stderr)
    else:
        print(json.dumps(result))
    return 0 if result.get("ok") else 1


def setup_status() -> dict:
    if not LAUNCHER.exists():
        return {"available": False, "logged_in": False, "plan": None,
                "detail": f"Plugin launcher missing: {LAUNCHER}", "login_command": None}
    login_command = [sys.executable, str(PLUGIN_DIR / "cursor_setup.py"), "login"]
    if not runtime_present():
        # The login command installs the runtime first, so setup can still proceed on a TTY.
        return {"available": True, "logged_in": False, "plan": None,
                "detail": "Cursor SDK runtime not installed yet. Install it and sign in to Cursor.",
                "login_command": login_command}
    state = auth_state()
    if state["logged_in"]:
        who = state.get("email") or state.get("source")
        return {"available": True, "logged_in": True, "plan": who, "detail": f"Signed in to Cursor ({who}).",
                "login_command": login_command}
    detail = ("Cursor login expired. Sign in to Cursor again." if state.get("expired")
              else "Not signed in to Cursor. Sign in with your Cursor account.")
    return {"available": True, "logged_in": False, "plan": None, "detail": detail, "login_command": login_command}
