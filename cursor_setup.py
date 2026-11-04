"""Setup CLI for the cursor-subscription-directsdk Hermes provider.

    python3 cursor_setup.py install   # private .venv + hash-pinned cursor-sdk
    python3 cursor_setup.py status    # runtime state

Sign in with `hermes auth add cursor-subscription-directsdk` (Cursor's browser login; the key is
stored in Hermes' credential pool), or set CURSOR_API_KEY.
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
    if command == "status":
        print(json.dumps({"runtime_installed": rt.runtime_installed(), "venv": str(rt.VENV_DIR),
                          "sdk": rt.SDK_REQUIREMENT, "state_dir": str(rt.state_dir()),
                          "sign_in": f"hermes auth add {rt.PROVIDER}"}, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
