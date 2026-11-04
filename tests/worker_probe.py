"""Drive sidecar/worker.py directly over JSON lines (debug aid)."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
env = dict({k: v for k, v in os.environ.items() if k != "CURSOR_API_KEY"}, CURSOR_SDK_LOG=os.environ.get("CURSOR_SDK_LOG", "info"))
proc = subprocess.Popen([str(ROOT / "bin" / "cursor-sidecar")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=sys.stderr, text=True, env=env)
events = []


def reader():
    for line in proc.stdout:
        msg = json.loads(line)
        print(time.strftime("%X"), "<-", json.dumps(msg)[:300], flush=True)
        events.append(msg)


threading.Thread(target=reader, daemon=True).start()


def send(obj):
    print(time.strftime("%X"), "->", json.dumps(obj)[:300], flush=True)
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def wait_for(req, kinds=("tool_calls", "done", "error"), timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        for e in events:
            if e.get("id") == req and e.get("type") in kinds:
                return e
        time.sleep(0.1)
    raise SystemExit("timeout")


tools = [{"name": "get_weather", "description": "Weather for a city", "schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
send({"op": "turn", "id": 1, "session": "s1", "create": {"model": "composer-2.5", "params": [], "tools": tools,
                                                   "api_key": os.environ["CURSOR_API_KEY"]},
      "send": {"text": "Weather in Dallas? use the tool, then answer in one sentence."}, "results": None})
ev = wait_for(1)
calls = ev.get("calls") or []
send({"op": "turn", "id": 2, "session": "s1", "create": None, "send": None,
      "results": {c["id"]: {"content": '{"temp_f": 71}', "is_error": False} for c in calls}})
print("FINAL", wait_for(2))
send({"op": "shutdown"})
time.sleep(1)
