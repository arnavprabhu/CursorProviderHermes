"""Paths, runtime install and browser sign-in for the Cursor SDK this plugin drives.

The Cursor SDK (``cursor-sdk`` on PyPI, which bundles Cursor's own node bridge) lives in a private
venv at ``<plugin>/.venv`` (``python_runtime: external``) so it never touches Hermes' own
environment. It is installed from PyPI with ``--require-hashes`` against ``sidecar/requirements.lock``.

Credentials come only through Hermes: the runtime API key is whatever Hermes resolves for this
provider (``hermes auth add`` pool row, or ``CURSOR_API_KEY``) and is handed to the sidecar per
request. The browser sign-in runs Cursor's official SDK login with ``store: null``: Cursor mints a
named, expiring user API key and returns it over a pipe straight into Hermes' credential pool.
Nothing is written to Cursor's own credential file, and no vendor credential file is ever read.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROVIDER = "cursor-subscription-directsdk"
DISPLAY_NAME = "Cursor Subscription (DirectSDK)"
BASE_URL = "https://api.cursor.com"

PLUGIN_DIR = Path(__file__).resolve().parent
VENV_DIR = PLUGIN_DIR / ".venv"
LAUNCHER = PLUGIN_DIR / "bin" / "cursor-sidecar"
WORKER = PLUGIN_DIR / "sidecar" / "worker.py"
AUTH_HELPER = PLUGIN_DIR / "sidecar" / "cursor_login.mjs"
LOCKFILE = PLUGIN_DIR / "sidecar" / "requirements.lock"
# Pinned: the sidecar is qualified against this SDK (custom-tool callbacks, tools=["mcp"]).
SDK_REQUIREMENT = "cursor-sdk==1.3.1"
LOGIN_TIMEOUT_S = 600

INSTALL_HINT = (f"The Cursor SDK runtime is not installed. Run `hermes auth add {PROVIDER}` (or "
                f"`python3 {PLUGIN_DIR / 'cursor_setup.py'} install`).")
LOGGED_OUT_HINT = (f"Not signed in to Cursor. Run `hermes auth add {PROVIDER}` to sign in with your browser, "
                   "or set CURSOR_API_KEY to a Cursor user API key.")
REJECTED_HINT = (f"Cursor rejected the API key (revoked or expired). Run `hermes auth add {PROVIDER}` to sign "
                 f"in again, and `hermes auth remove {PROVIDER} <id>` to drop the dead key.")


def state_dir() -> Path:
    """Per-profile scratch state (model cache, the Cursor agent workspace)."""
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - standalone use (tests, cursor_setup.py)
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "cache" / PROVIDER


def venv_python() -> Path:
    return VENV_DIR / "bin" / "python"


def clean_env(extra=None):
    """Child environment: no Hermes venv leakage, and no ambient Cursor key (the key Hermes resolved
    for this request is the only one the sidecar may use)."""
    drop = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CURSOR_API_KEY")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(extra or {})
    return env


def runtime_present() -> bool:
    """Cheap check (no subprocess) for hot paths."""
    return venv_python().exists()


def runtime_installed() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    try:
        out = subprocess.run([str(py), "-I", "-c", "import cursor_sdk, sys; sys.stdout.write('ok')"],
                             capture_output=True, text=True, timeout=60, env=clean_env())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.stdout == "ok"


def _find_uv():
    found = shutil.which("uv")
    if found:
        return found
    for base in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (base / "uv").exists():
            return str(base / "uv")
    return None


def install(*, quiet: bool = False) -> None:
    """Create the private venv and install the hash-pinned Cursor SDK (about 200 MB, bundles node)."""
    say = (lambda *_: None) if quiet else (lambda msg: print(msg, flush=True))
    uv = _find_uv()
    if not venv_python().exists():
        say(f"Creating the Cursor SDK venv in {VENV_DIR} ...")
        if uv:
            subprocess.run([uv, "venv", "--quiet", "--python", sys.executable, str(VENV_DIR)],
                           check=True, env=clean_env())
        else:
            subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True, env=clean_env())
    say(f"Installing {SDK_REQUIREMENT} from PyPI (hash-pinned, about 200 MB) ...")
    if uv:
        cmd = [uv, "pip", "install", "--quiet", "--python", str(venv_python()), "--require-hashes",
               "-r", str(LOCKFILE)]
    else:
        cmd = [str(venv_python()), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
               "--require-hashes", "-r", str(LOCKFILE)]
    subprocess.run(cmd, check=True, env=clean_env())
    if not runtime_installed():
        raise RuntimeError("cursor-sdk did not import after install")
    say("Cursor SDK runtime ready.")


def _bridge_dir() -> Path | None:
    try:
        out = subprocess.run([str(venv_python()), "-I", "-c",
                              "import cursor_sdk, os; print(os.path.dirname(cursor_sdk.__file__))"],
                             capture_output=True, text=True, timeout=60, env=clean_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    base = Path(out.stdout.strip()) / "_vendor" / "bridge"
    return base if (base / "bin").is_dir() else None


def browser_login(*, open_browser: bool = True, key_name: str = "Hermes Agent") -> dict:
    """Cursor's official SDK browser login, without persistence on Cursor's side.

    Returns ``{"api_key", "email", "expires_at_ms"}``. The key travels over the helper's stdout
    pipe only; callers hand it to Hermes' credential pool and never print it.
    """
    bridge = _bridge_dir()
    if bridge is None:
        raise RuntimeError(INSTALL_HINT)
    node = bridge / "bin" / "node"
    sdk = (bridge / "node_modules" / "@cursor" / "sdk" / "dist" / "esm" / "index.js").as_uri()
    env = clean_env({"CURSOR_LOGIN_KEY_NAME": key_name})
    if not open_browser:
        env["NO_OPEN_BROWSER"] = "1"
    try:
        proc = subprocess.run([str(node), str(AUTH_HELPER), sdk], env=env, text=True,
                              stdout=subprocess.PIPE, timeout=LOGIN_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Cursor login timed out after {LOGIN_TIMEOUT_S}s") from exc
    try:
        result = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        result = {"ok": False, "error": "the login helper returned no result"}
    if not result.get("ok") or not result.get("api_key"):
        raise RuntimeError(f"Cursor login failed: {result.get('error') or 'unknown error'}")
    return {"api_key": result["api_key"], "email": result.get("email"),
            "expires_at_ms": result.get("expires_at_ms")}
