"""List local Cursor agents the sidecar created (debug aid): .runtime/venv/bin/python -I tests/list_agents.py"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
from worker import api_key  # noqa: E402
from cursor_sdk import CursorClient  # noqa: E402

wd = os.environ.get("CURSOR_SUBSCRIPTION_WORKDIR") or os.path.join(
    os.path.expanduser("~"), ".cache", "hermes-cursor-subscription", "workspace")
with CursorClient.launch_bridge(workspace=wd) as client:
    page = client.agents.list(runtime="local", cwd=wd)
    rows = list(page.auto_paging_iter())
    print(len(rows), "agents")
    for info in sorted(rows, key=lambda r: r.created_at or ""):
        print(info.created_at, info.agent_id, repr(info.name), info.status)
