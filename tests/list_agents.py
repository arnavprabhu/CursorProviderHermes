"""List local Cursor agents the sidecar created (debug aid): .venv/bin/python -I tests/list_agents.py"""
import os

from cursor_sdk import CursorClient

wd = os.environ.get("CURSOR_SUBSCRIPTION_WORKDIR") or os.path.join(
    os.path.expanduser("~"), ".hermes", "cache", "cursor-subscription-directsdk", "workspace")
with CursorClient.launch_bridge(workspace=wd) as client:
    rows = list(client.agents.list(runtime="local", cwd=wd).auto_paging_iter())
    print(len(rows), "agents")
    for info in sorted(rows, key=lambda r: r.created_at or ""):
        print(info.created_at, info.agent_id, repr(info.name), info.status)
