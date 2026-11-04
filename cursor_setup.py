"""Setup CLI for the cursor-subscription Hermes provider.

    python cursor_setup.py install   # private venv + pinned cursor-sdk
    python cursor_setup.py login     # install if needed, then Cursor's browser login
    python cursor_setup.py status    # runtime + login state (never prints the key)
    python cursor_setup.py logout    # forget the stored Cursor SDK login
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cursor_runtime as rt  # noqa: E402


def main(argv):
    command = argv[1] if len(argv) > 1 else "status"
    if command == "install":
        rt.install()
        return 0
    if command == "login":
        if not rt.runtime_installed():
            rt.install()
        return rt.run_auth("login")
    if command == "logout":
        return rt.run_auth("logout")
    if command == "status":
        state = rt.auth_state()
        print(json.dumps({"runtime_installed": rt.runtime_installed(), "venv": str(rt.VENV_DIR),
                          "sdk": rt.SDK_REQUIREMENT, **{k: v for k, v in state.items()}}, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
