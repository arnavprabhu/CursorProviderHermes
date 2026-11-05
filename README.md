# Cursor Subscription (DirectSDK) — Hermes model-provider plugin

Use the models on your existing **Cursor plan** (Claude, GPT, Gemini, Grok, Composer, Auto, ...) as a
Hermes provider. Hermes keeps its own agent loop, tools, approvals, memory, skills and compaction;
Cursor only supplies the model, billed to your Cursor account.

Provider id: `cursor-subscription-directsdk` · display name: **Cursor Subscription (DirectSDK)**.
Modelled on `claude-subscription-directsdk`, built on Cursor's official SDK (`cursor-sdk`).

What sets it apart from prompt-level bridges: Hermes' tools reach the model as **native Cursor
custom tools**. There is no `<tool_call>` text protocol to parse. Parallel tool calls arrive as one
batch, and a tool round continues the same Cursor run, so the model keeps its native multi-step
context and Cursor's prompt cache stays warm.

## How it works

```
Hermes agent loop ──chat.completions.create()──► transport.py ──JSON lines──► sidecar/worker.py
       ▲                                                                         │ cursor-sdk (official)
       │  tool_calls / text / reasoning                                          ▼
       └──────────── Hermes runs the tools ◄── parked custom-tool callbacks ◄── Cursor agent run
```

* The sidecar runs Cursor's official Python SDK (`cursor-sdk`, which bundles Cursor's node bridge) in
  a private venv at `<plugin>/.venv` (`python_runtime: external`), so nothing is installed into
  Hermes' own environment.
* Each Hermes conversation maps to one Cursor **local agent** with every Cursor built-in tool switched
  off (`tools=["mcp"]`: no shell, no file reads or edits, no search). Hermes' tools are registered as
  Cursor *custom tools*, and only those are offered to the model.
* When the model calls a tool, the custom-tool callback **parks**. The plugin returns
  `finish_reason="tool_calls"` to Hermes, Hermes runs the tool (approvals, hooks, sandbox and all)
  and sends the result back on its next request. The plugin hands it to the parked callback and the
  same Cursor run continues. Tools never execute inside the plugin or inside Cursor.
* A new user message after a finished turn continues the same Cursor agent.
* If Hermes' history no longer matches what the agent saw (compression, `/retry`, edits, a model or
  toolset switch, a gateway restart), the plugin starts a fresh agent seeded with Hermes' system
  prompt and a transcript of the history. That path is always correct, just colder.
* Auxiliary calls (titles, compression summaries) run as throwaway one-shot agents.

## Requirements

* Hermes Agent 0.22.6+, Linux or macOS.
* A Cursor account with a plan.
* About 200 MB of disk for the SDK runtime (`cursor-sdk` 1.3.1 wheels include Cursor's node bridge),
  installed from PyPI with `--require-hashes` against [`sidecar/requirements.lock`](sidecar/requirements.lock).
  `uv` is used if available, else `python -m venv` + pip.

## Install

```sh
hermes plugins install cursor-subscription-directsdk    # catalog (once listed), or: arnavprabhu/CursorProviderHermes
hermes auth add cursor-subscription-directsdk           # installs the SDK runtime, then Cursor's browser sign-in
hermes model                                            # pick "Cursor Subscription (DirectSDK)"
```

It's a `kind: model-provider` plugin, so it needs no `plugins.enabled` entry. `hermes model` also
installs the runtime and offers the browser sign-in on first use. Per-profile installs and
credentials are profile-scoped, as with any provider:
`hermes --profile work auth add cursor-subscription-directsdk`.

After installing or updating while a gateway is running, restart it from a separate shell:
`hermes gateway restart`.

Try it without changing your default model:

```sh
hermes chat --provider cursor-subscription-directsdk -m composer-2.5 -q 'Reply with exactly: OK'
```

### Sign-in and credentials

`hermes auth add cursor-subscription-directsdk` runs Cursor's official SDK sign-in
(`Cursor.auth.login`, with Cursor-side persistence turned off). It prints a `cursor.com` URL you
can open in any browser. Approving it mints a named, expiring (90-day) Cursor user API key,
**Hermes Agent**, which you can see and revoke under Cursor Dashboard → API Keys. The key goes over
a pipe straight into **Hermes' credential pool** for this provider. It is not written to Cursor's
credential file, and the plugin never prints or logs it.

To use a key you created yourself instead, run `hermes auth add cursor-subscription-directsdk
--type api-key`, or set `CURSOR_API_KEY` in your profile's `.env`. `hermes auth status`, `hermes auth
list` and `hermes auth remove` work as for any provider.

If Cursor rejects a key (revoked in the dashboard, or past its 90 days), the request fails with
"Cursor rejected the API key" as a 401, so Hermes moves on to the next key in the pool. Sign in again
with `hermes auth add cursor-subscription-directsdk`, then drop the dead row with `hermes auth
remove cursor-subscription-directsdk <id>`.

At request time the plugin uses only the key Hermes resolves for the active profile. It passes the
key to the sidecar inside each request (over stdin, never the environment). The sidecar, Cursor's
node bridge and the sign-in helper get an environment rebuilt from an allowlist
(`CHILD_ENV_ALLOWLIST` in `cursor_runtime.py`: `PATH`, `HOME`, user and locale, `TMPDIR`, TLS and
proxy settings, the display variables the sign-in browser needs, and this plugin's own
`CURSOR_SUBSCRIPTION_*` / `CURSOR_SDK_LOG`), so other providers' keys, bot tokens and an ambient
`CURSOR_API_KEY` never reach Cursor's code. The plugin reads no vendor credential files.

## Models

The picker shows your account's live Cursor catalog, cached for 6 h in
`$HERMES_HOME/cache/cursor-subscription-directsdk/models.json`, with **Cursor Auto** first as
`cursor-auto`, then `composer-2.5`, `claude-opus-5-5`, `claude-sonnet-5-5`, `gpt-5.5`, `gpt-5.6-sol`,
`gemini-3.1-pro`, `grok-4.7`, and the rest. Aliases: `auto`, `composer`, `opus`, `sonnet`, `haiku`,
`gpt`, `gemini`, `grok`.

### Cursor Auto

`cursor-auto` lets Cursor pick the model for each request, the same as Auto in the Cursor app
(Cursor's own id for it is `default`, which is what goes on the wire).

```sh
hermes chat --provider cursor-subscription-directsdk -m cursor-auto -q 'Reply with exactly: OK'
/model auto            # in a session; `auto` is an alias for cursor-auto
```

* It is listed as `cursor-auto` rather than `auto` because Hermes treats a bare `auto` model as
  "inherit / auto-detect" in its auxiliary settings. `/model auto`, `-m auto` and an older config
  holding `default` all still resolve to Cursor Auto.
* Cursor does not report which model Auto chose, so Hermes shows `cursor-auto` throughout.
* Auto has no Cursor parameters, so `/reasoning` and `@param` pins do not apply to it, and Hermes
  budgets against a 200K context window.
* Tool calling works the same way as on a fixed model (verified live: parallel and chained tool
  calls, follow-ups and replay).

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
| `CURSOR_API_KEY` | Cursor user API key, used when the credential pool has no row (standard Hermes env route). |
| `CURSOR_SUBSCRIPTION_TOOL_TIMEOUT` | Seconds a parked tool call waits for Hermes (default 7200). |
| `CURSOR_SUBSCRIPTION_EVENT_TIMEOUT` | Seconds of model silence before a request fails (default 900). |
| `CURSOR_SUBSCRIPTION_KEEP_AGENTS` | Keep Cursor's local agent transcripts instead of deleting them on close. |
| `CURSOR_SUBSCRIPTION_DEBUG` | Verbose sidecar logging (goes to the Hermes log at debug level). |

## What this plugin does on your machine (disclosure)

* **Network:** conversation text, Hermes' system prompt, tool schemas, tool results and user images
  are sent to Cursor (`api.cursor.com`) through Cursor's SDK, using your Cursor key and plan. Setup
  downloads `cursor-sdk` and its dependencies from PyPI (hash-pinned). Sign-in talks to
  `cursor.com`. Nothing else is contacted, and there is no telemetry.
* **Processes:** one long-lived sidecar per Hermes profile in use by a Hermes process (`.venv`
  python running `sidecar/worker.py`), which starts Cursor's bundled node bridge listening on
  loopback. Both exit with Hermes. The sign-in runs the same bundled node once.
* **Files:** the venv at `<plugin>/.venv`; a model cache and an empty Cursor agent workspace under
  `$HERMES_HOME/cache/cursor-subscription-directsdk/` of the active profile (a multi-profile gateway
  keeps each profile's agent state apart). Cursor's SDK keeps local agent transcripts
  for live conversations; the plugin deletes each agent when its conversation closes (set
  `CURSOR_SUBSCRIPTION_KEEP_AGENTS=1` to keep them).
* **Credentials:** stored only by Hermes (credential pool / `.env`), as described above.
* **Tools and approvals:** Cursor's built-in tools are disabled, and agents are created with no MCP
  servers and no Cursor settings sources (no project/user `.cursor` config), so Hermes' tools are
  the only tools. Every tool call is executed by Hermes under its normal approval rules; the plugin
  never auto-approves anything.
* **Unattended runs:** the browser sign-in only runs from `hermes auth add` or an interactive
  `hermes model`, and it times out after 10 minutes. Without a credential, requests fail fast with
  the sign-in hint instead of waiting.

## Limitations (read these)

* **Cursor's harness prompt stays.** The SDK has no system-prompt override, so Cursor's own agent
  prompt (about 10–15K tokens) comes first, and Hermes' system prompt is delivered inside the first
  user message, wrapped in an instruction block that tells the model it takes precedence.
* **Usage is reported per run, not per call.** Cursor sums every model step of a run into one usage
  record, which is not the size of any single prompt. The plugin therefore leaves per-call `usage`
  unset, so Hermes falls back to its own context estimate and `/usage` and cost show `n/a`. Spend is
  on your Cursor dashboard under the **SDK** tag.
* **Slow tools:** if a parked tool call takes longer than about 270 s, Cursor retries the step once
  with the same call ID. The plugin dedupes by call ID, so the tool still runs once in Hermes.
  Verified with 400 s and 600 s tools.
* **Images:** base64 images in user messages go to the model natively. Images inside tool results
  are passed as `[image]` text (use Hermes' `vision_analyze`).
* **Structured output** (`response_format`) is instruction-based; Hermes validates the reply.
  `temperature`, `top_p`, `stop` and `max_tokens` are ignored (Cursor does not expose them).
* **Replay is a text transcript.** After a history rewrite the new agent sees earlier turns as text,
  not native tool calls, and starts with a cold cache.
* Cursor may auto-summarize its own context near its window limit, independently of Hermes.
* Windows is not supported yet (the launcher is a POSIX shell script).
* Experimental: interfaces of `cursor-sdk` 1.3.1 (custom-tool callbacks, `tools=["mcp"]`, stream
  updates) are relied on directly. The pin moves only with a re-tested release of this plugin.

## Tests

```sh
HPY=~/.hermes/hermes-agent/venv/bin/python                    # Hermes' own python
python3 -m unittest discover -s tests -p 'test_*.py'          # offline: planner, catalog, prompts, credentials, env
.venv/bin/python -I -m unittest discover -s tests -p 'test_*.py'  # the same, plus the agent-options check
$HPY tests/live_loop.py composer-2.5                          # live: tool loop, follow-up, replay (uses your plan)
$HPY tests/live_loop.py cursor-auto                           # the same on Cursor Auto
$HPY tests/worker_probe.py                                    # live: raw sidecar protocol
.venv/bin/python -I tests/list_agents.py                      # list local Cursor agents the sidecar created
```

The live scripts take the key from Hermes' credential pool inside the process (`tests/_hermes_key.py`)
and never print it. `CURSOR_API_KEY` set for that one command also works. Don't `export` a key in a
shell an AI agent drives, because agent terminals can snapshot their environment to disk.

## Files

| File | Role |
| --- | --- |
| `__init__.py` | Provider profile: credentials via Hermes, `hermes auth add` / `hermes model` steps, model list, context length, client. |
| `transport.py` | OpenAI-shaped client, conversation registry (resume / follow-up / replay), sidecar manager. |
| `sidecar/worker.py` | Runs in the private venv; drives `cursor-sdk`, parks custom-tool callbacks. |
| `sidecar/cursor_login.mjs` | Cursor's SDK browser sign-in (no Cursor-side persistence), run with the SDK's bundled node. |
| `sidecar/requirements.lock` | Hash-pinned runtime requirements. |
| `cursor_runtime.py`, `cursor_setup.py` | Runtime install, sign-in helper, setup CLI. |
| `cursor_catalog.py` | Cached live catalog, context windows, effort mapping, `@param` pins. |
| `bin/cursor-sidecar` | Launches the sidecar with the private venv in an allowlisted environment. |

License: MIT. `cursor-sdk` is Cursor's separately distributed package, governed by Cursor's own
license and terms.
