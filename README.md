# Cursor Subscription (Experimental) — Hermes model-provider plugin

Use the models on your existing **Cursor plan** (Claude, GPT, Gemini, Grok, Composer, Auto, ...) as a
Hermes provider. Hermes keeps its own agent loop, tools, approvals, memory, skills and compaction;
Cursor only supplies the model, billed to your Cursor account.

Provider id: `cursor-subscription` · display name: **Cursor Subscription (Experimental)**.
Modelled on `claude-subscription-directsdk`, but built on Cursor's official SDK instead of a CLI.

## How it works

```
Hermes agent loop ──chat.completions.create()──► transport.py ──JSON lines──► sidecar/worker.py
       ▲                                                                         │ cursor-sdk (official)
       │  tool_calls / text / reasoning                                          ▼
       └──────────── Hermes runs the tools ◄── parked custom-tool callbacks ◄── Cursor agent run
```

* The sidecar runs Cursor's official Python SDK (`cursor-sdk`, which bundles Cursor's node bridge) in
  a private venv under `.runtime/`, so nothing is installed into Hermes' own environment.
* Each Hermes conversation maps to one Cursor **local agent** with every Cursor built-in tool switched
  off (`tools=["mcp"]`: no shell, no file edits, no search). Hermes' tools are registered as Cursor
  *custom tools*.
* When the model calls a tool, the custom-tool callback **parks**: the plugin returns
  `finish_reason="tool_calls"` to Hermes, Hermes runs the tool (approvals, hooks, sandbox and all)
  and sends the result back on its next request. The plugin hands it to the parked callback and the
  same Cursor run continues, keeping the model's native multi-step context and Cursor's prompt cache
  warm. Parallel tool calls arrive as one batch.
* A new user message after a finished turn continues the same Cursor agent.
* If Hermes' history no longer matches what the agent saw (compression, `/retry`, edits, a model or
  toolset switch, a gateway restart), the plugin starts a fresh agent seeded with Hermes' system
  prompt and a transcript of the history. That path is always correct, just colder.
* Auxiliary calls (titles, compression summaries) run as throwaway one-shot agents.

## Requirements

* Hermes Agent 0.21.4+ (qualified on 0.22.6), Linux or macOS.
* A Cursor account with a plan. `cursor-sdk` 1.3.1 is pinned and installed automatically (~200 MB,
  includes node). `uv` is used if available, else `python -m venv` + pip.

## Install

```sh
hermes plugins install arnavprabhu/CursorProviderHermes     # or: symlink this dir to ~/.hermes/plugins/cursor-subscription
hermes model                                               # pick "Cursor Subscription (Experimental)"
```

`hermes model` installs the SDK runtime and starts Cursor's browser login on first use. It's a
`kind: model-provider` plugin, so it needs no `plugins.enabled` entry. Per-profile installs go under
`~/.hermes/profiles/<profile>/plugins/`.

Manual setup, if you prefer:

```sh
python3 cursor_setup.py login    # installs .runtime/venv + cursor-sdk if missing, then browser login
python3 cursor_setup.py status   # runtime + login state (never prints the key)
python3 cursor_setup.py logout
hermes --provider cursor-subscription -m claude-opus-5-5
```

### Login

`login` runs Cursor's own SDK sign-in (`Cursor.auth.login`). It prints a `cursor.com` URL you can
open in any browser. Approving it mints a named, expiring (90-day) user API key, **Hermes Agent**,
which you can see and revoke under Cursor Dashboard → API Keys. The key lives in
`~/.cursor/sdk/auth.json` (0600). The plugin never prints it, copies it, or stores it in Hermes.
Alternatively, set `CURSOR_API_KEY` (a user API key from the dashboard) in `~/.hermes/.env`.

## Models

The picker shows your account's live Cursor catalog (cached for 6 h in `.runtime/models.json`):
`default` (Auto), `composer-2.5`, `claude-opus-5-5`, `claude-sonnet-5-5`, `gpt-5.5`, `gpt-5.6-sol`,
`gemini-3.1-pro`, `grok-4.7`, and the rest. Aliases: `auto`, `composer`, `opus`, `sonnet`, `haiku`,
`gpt`, `gemini`, `grok`.

* **Context window** is the model's default Cursor variant (for example 1M for Opus 5.5 and GPT-5.5,
  200K when Cursor publishes no context parameter). Hermes compresses against that number.
* **Reasoning effort** (`agent.reasoning_effort`, `/reasoning`) maps onto whichever parameter the
  model has (`effort`, `reasoning_effort` or `reasoning`), clamped down to the nearest offered
  level. Turning reasoning off sets `thinking=false` where the model has that switch.
* **Pin any Cursor model parameter** in the model id after `@`:
  `claude-opus-5-5@context=300k,fast=true`, `gpt-5.5@reasoning=high`. Pins beat Hermes' reasoning
  setting.

## Configuration (environment)

| Variable | Effect |
| --- | --- |
| `CURSOR_API_KEY` | Use this user API key instead of the SDK login file. |
| `CURSOR_SUBSCRIPTION_COMMAND` | Alternative sidecar launcher (default `bin/cursor-sidecar`). |
| `CURSOR_SUBSCRIPTION_PYTHON` | Python that already has `cursor-sdk` (skips the private venv). |
| `CURSOR_SUBSCRIPTION_TOOL_TIMEOUT` | Seconds a parked tool call waits for Hermes (default 7200). |
| `CURSOR_SUBSCRIPTION_EVENT_TIMEOUT` | Seconds of model silence before a request fails (default 900). |
| `CURSOR_SUBSCRIPTION_KEEP_AGENTS` | Keep Cursor's local agent transcripts instead of deleting them on close. |
| `CURSOR_SUBSCRIPTION_DEBUG` | Verbose sidecar logging (goes to the Hermes log at debug level). |

## Limitations (read these)

* **Cursor's harness prompt stays.** The SDK has no system-prompt override, so Cursor's own agent
  prompt (about 10–15K tokens) comes first, and Hermes' system prompt is delivered inside the first
  user message, wrapped in an instruction block that tells the model it takes precedence.
* **Usage is reported per run, not per call.** Cursor sums every model step of a run into one usage
  record, which is not the size of any single prompt. The plugin therefore leaves per-call `usage`
  unset (Hermes falls back to its own context estimate; `/usage` and cost show `n/a`). Spend is on
  your Cursor dashboard under the **SDK** tag.
* **Slow tools:** if a parked tool call takes longer than ~270 s, Cursor retries the step once with
  the same call id. The plugin dedupes by call id, so the tool still runs once in Hermes. Verified
  with 400 s and 600 s tools.
* **Images:** base64 images in user messages go to the model natively. Images inside tool results
  are passed as `[image]` text (use Hermes' `vision_analyze`).
* **Structured output** (`response_format`) is instruction-based; Hermes validates the reply.
  `temperature`, `top_p`, `stop` and `max_tokens` are ignored (Cursor does not expose them).
* **Replay is a text transcript.** After a history rewrite the new agent sees earlier turns as text,
  not native tool calls, and starts with a cold cache.
* Cursor may auto-summarize its own context near its window limit, independently of Hermes.
* Windows is not supported yet (the launcher is a POSIX shell script).
* Experimental: interfaces of `cursor-sdk` 1.3.1 (custom-tool callbacks, `tools=["mcp"]`, stream
  updates) are relied on directly. Bump `SDK_REQUIREMENT` deliberately and re-run the live tests.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py'          # offline: planner, catalog, prompts, auth
python3 tests/live_loop.py composer-2.5                       # live: tool loop, follow-up, replay (uses your plan)
python3 tests/worker_probe.py                                 # live: raw sidecar protocol
.runtime/venv/bin/python -I tests/list_agents.py              # list local Cursor agents the sidecar created
```

## Files

| File | Role |
| --- | --- |
| `__init__.py` | Provider profile Hermes queries (setup status, model list, context length, client). |
| `transport.py` | OpenAI-shaped client, conversation registry (resume / follow-up / replay), sidecar manager. |
| `sidecar/worker.py` | Runs in the private venv; drives `cursor-sdk`, parks custom-tool callbacks. |
| `sidecar/cursor_auth.mjs` | Cursor SDK login/logout/status, run with the SDK's bundled node. |
| `cursor_runtime.py`, `cursor_setup.py` | Runtime install, login state, setup CLI. |
| `cursor_catalog.py` | Cached live catalog, context windows, effort mapping, `@param` pins. |
| `bin/cursor-sidecar` | The external-process command Hermes resolves. |

License: MIT.
