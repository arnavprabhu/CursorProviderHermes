"""Live end-to-end check of the transport against a real Cursor account (no Hermes needed).

Simulates Hermes' loop: create() -> run requested tools locally -> create() with results -> ...
then a follow-up user turn on the same conversation, then a replayed (rewritten) history.

    .runtime/venv/bin/python -I tests/live_loop.py [model]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transport  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "composer-2.5"
TOOLS = [
    {"type": "function", "function": {"name": "get_weather", "description": "Current weather for a city.",
                                      "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                     "required": ["city"]}}},
    {"type": "function", "function": {"name": "add", "description": "Add two integers.",
                                      "parameters": {"type": "object", "properties": {"a": {"type": "integer"},
                                                                                      "b": {"type": "integer"}},
                                                     "required": ["a", "b"]}}},
]
WEATHER = {"dallas": 71, "austin": 77, "houston": 80}


def run_tool(name, args):
    if name == "get_weather":
        return json.dumps({"city": args["city"], "temp_f": WEATHER.get(args["city"].lower(), 60)})
    if name == "add":
        return str(int(args["a"]) + int(args["b"]))
    return "unknown tool"


def step(client, messages, stream=True):
    t0 = time.time()
    if stream:
        text, calls, finish = [], None, None
        for chunk in client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS, stream=True):
            delta = chunk.choices[0].delta
            if delta.content:
                text.append(delta.content)
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
                message = chunk._response.choices[0].message.model_dump()
        print(f"  [{finish}] {time.time() - t0:.1f}s text={''.join(text)[:160]!r}")
    else:
        response = client.chat.completions.create(model=MODEL, messages=messages, tools=TOOLS)
        message = response.choices[0].message.model_dump()
        print(f"  [{response.choices[0].finish_reason}] {time.time() - t0:.1f}s text={(message['content'] or '')[:160]!r}")
    message = {k: v for k, v in message.items() if v is not None}
    messages.append(message)
    for call in message.get("tool_calls") or []:
        args = json.loads(call["function"]["arguments"])
        result = run_tool(call["function"]["name"], args)
        print(f"    tool {call['function']['name']}({args}) -> {result}")
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return bool(message.get("tool_calls"))


def loop(client, messages, stream=True, limit=8):
    for _ in range(limit):
        if not step(client, messages, stream):
            return
    raise SystemExit("tool loop did not finish")


def main():
    client = transport.Client()
    system = "You are a terse assistant. Always use tools for weather and arithmetic; never guess."
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": "Weather in Dallas and Austin? Then add the two temperatures with the add tool."}]
    print("turn 1 (tool loop, parked-run resume):")
    loop(client, messages)
    sid1 = next(iter(transport._conversations.values())).sid
    print("turn 2 (follow-up on the same Cursor agent):")
    messages.append({"role": "user", "content": "Now Houston's weather. One short sentence."})
    loop(client, messages, stream=False)
    sid2 = next(iter(transport._conversations.values())).sid
    assert sid1 == sid2, "follow-up should reuse the same Cursor agent"
    print("turn 3 (history rewritten -> replay into a new agent):")
    rewritten = [messages[0], {"role": "user", "content": "Summary of earlier: Dallas 71F, Austin 77F, Houston 80F."},
                 {"role": "assistant", "content": "Noted."},
                 {"role": "user", "content": "Which of the three cities was warmest? Answer with just the city."}]
    loop(client, rewritten)
    print("OK sessions:", [c.sid for c in transport._conversations.values()])


if __name__ == "__main__":
    main()
