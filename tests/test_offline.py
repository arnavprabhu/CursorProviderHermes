"""Offline tests (no Cursor account, no Hermes): python -m unittest discover -s tests -p 'test_*.py'"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="cursor-sub-test-")  # model cache lands here

import cursor_catalog as catalog  # noqa: E402
import cursor_runtime as rt  # noqa: E402
import transport  # noqa: E402

ROWS = [
    {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
     "parameters": [{"id": "context", "values": ["300k", "1m"]},
                    {"id": "effort", "values": ["low", "medium", "high", "xhigh", "max"]},
                    {"id": "fast", "values": ["false", "true"]}],
     "default_params": [["context", "1m"], ["effort", "medium"], ["fast", "false"]]},
    {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8",
     "parameters": [{"id": "thinking", "values": ["false", "true"]}, {"id": "context", "values": ["300k", "1m"]},
                    {"id": "effort", "values": ["low", "medium", "high", "xhigh", "max"]}],
     "default_params": [["thinking", "true"], ["context", "1m"], ["effort", "high"]]},
    {"id": "gpt-5.5", "display_name": "GPT-5.5",
     "parameters": [{"id": "context", "values": ["272k", "1m"]},
                    {"id": "reasoning", "values": ["none", "low", "medium", "high", "extra-high"]}],
     "default_params": [["context", "1m"], ["reasoning", "medium"]]},
    {"id": "composer-2.5", "display_name": "Composer 2.5", "parameters": [{"id": "fast", "values": ["false", "true"]}],
     "default_params": [["fast", "true"]]},
    {"id": "default", "display_name": "Auto", "parameters": [], "default_params": []},
]


class CatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog.store(ROWS)

    def test_context_length_follows_default_variant_and_pins(self):
        self.assertEqual(catalog.context_length("claude-opus-5-5"), 1_000_000)
        self.assertEqual(catalog.context_length("claude-opus-5-5@context=300k"), 300_000)
        self.assertEqual(catalog.context_length("gpt-5.5@context=272k"), 272_000)
        self.assertEqual(catalog.context_length("composer-2.5"), catalog.DEFAULT_CONTEXT)
        self.assertEqual(catalog.context_length("opus"), 1_000_000)  # alias
        self.assertIsNone(catalog.context_length("not-a-model"))

    def test_no_reasoning_sends_no_params(self):
        self.assertEqual(catalog.resolve_params("claude-opus-5-5"), ("claude-opus-5-5", []))

    def test_effort_overrides_on_top_of_default_variant(self):
        model, params = catalog.resolve_params("claude-opus-5-5", {"enabled": True, "effort": "high"})
        self.assertEqual(model, "claude-opus-5-5")
        self.assertEqual(dict(params), {"context": "1m", "effort": "high", "fast": "false"})

    def test_effort_vocabulary_mapping_and_clamp(self):
        self.assertEqual(dict(catalog.resolve_params("gpt-5.5", {"effort": "xhigh"})[1])["reasoning"], "extra-high")
        self.assertEqual(dict(catalog.resolve_params("gpt-5.5", {"effort": "max"})[1])["reasoning"], "extra-high")
        self.assertEqual(dict(catalog.resolve_params("gpt-5.5", {"enabled": False})[1])["reasoning"], "none")

    def test_disable_turns_thinking_off_when_offered(self):
        params = dict(catalog.resolve_params("claude-opus-4-8", {"enabled": False})[1])
        self.assertEqual(params["thinking"], "false")
        self.assertEqual(params["effort"], "high")  # no "none" effort offered: stays at the default

    def test_pins_beat_reasoning(self):
        params = dict(catalog.resolve_params("claude-opus-5-5@effort=max,fast=true", {"effort": "low"})[1])
        self.assertEqual(params["effort"], "max")
        self.assertEqual(params["fast"], "true")

    def test_alias_resolution(self):
        self.assertEqual(catalog.split_model("sonnet@context=300k"), ("claude-sonnet-5-5", {"context": "300k"}))

    def test_auto_is_cursor_auto_to_hermes_and_default_on_the_wire(self):
        # Every spelling a user or an older config might hold reaches Cursor as `default`.
        for spelling in ("cursor-auto", "auto", "default"):
            self.assertEqual(catalog.resolve_params(spelling), ("default", []), spelling)
        # Auto has no parameters, so Hermes' reasoning setting adds nothing to it.
        self.assertEqual(catalog.resolve_params("cursor-auto", {"effort": "high"}), ("default", []))
        self.assertEqual(catalog.context_length("cursor-auto"), catalog.DEFAULT_CONTEXT)

    def test_picker_lists_auto_first_as_cursor_auto(self):
        ids = catalog.model_ids()
        self.assertEqual(ids[0], "cursor-auto")
        self.assertNotIn("default", ids)
        self.assertEqual(sorted(ids[1:]), sorted(r["id"] for r in ROWS if r["id"] != "default"))
        self.assertEqual(catalog.FALLBACK_MODELS[0], "cursor-auto")
        # Never the bare sentinel Hermes' auxiliary client treats as "inherit / auto-detect".
        self.assertNotIn("auto", ids + list(catalog.FALLBACK_MODELS))


def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.rest = [{"role": "user", "content": "weather?"}]
        self.conv = transport.Conversation("s1", ("m", ()), "sig")

    def _published(self, calls, text=""):
        assistant = {"role": "assistant", "content": text or None, "tool_calls": calls}
        self.conv.consumed = [transport.digest(m) for m in self.rest] + [transport.digest(assistant)]
        self.conv.pending = [{"id": c["id"]} for c in calls]
        return assistant

    def plan(self, rest):
        return transport._plan(self.conv, [transport.digest(m) for m in rest], rest, ("m", ()), "sig")

    def test_resume_needs_every_pending_result(self):
        calls = [call("a", "get_weather", {"city": "Dallas"}), call("b", "get_weather", {"city": "Austin"})]
        assistant = self._published(calls)
        partial = self.rest + [assistant, {"role": "tool", "tool_call_id": "a", "content": "71"}]
        self.assertIsNone(self.plan(partial))
        full = partial + [{"role": "tool", "tool_call_id": "b", "content": "77"}]
        mode, results, _ = self.plan(full)
        self.assertEqual(mode, "resume")
        self.assertEqual({k: v["content"] for k, v in results.items()}, {"a": "71", "b": "77"})

    def test_resume_tolerates_reserialized_arguments(self):
        assistant = self._published([call("a", "add", {"a": 1, "b": 2})])
        hermes_copy = dict(assistant, tool_calls=[{"id": "a", "type": "function",
                                                   "function": {"name": "add", "arguments": '{"b":2, "a":1}'}}],
                           reasoning_content="thinking...")
        rest = self.rest + [hermes_copy, {"role": "tool", "tool_call_id": "a", "content": "3"}]
        self.assertEqual(self.plan(rest)[0], "resume")

    def test_user_message_after_results_rides_on_last_result(self):
        assistant = self._published([call("a", "add", {"a": 1, "b": 2})])
        rest = self.rest + [assistant, {"role": "tool", "tool_call_id": "a", "content": "3"},
                            {"role": "user", "content": "also say hi"}]
        mode, results, _ = self.plan(rest)
        self.assertEqual(mode, "resume")
        self.assertIn("also say hi", results["a"]["content"])

    def test_followup_after_finished_run(self):
        final = {"role": "assistant", "content": "It is 71F."}
        self.conv.consumed = [transport.digest(m) for m in self.rest + [final]]
        rest = self.rest + [final, {"role": "user", "content": "and Austin?"}]
        self.assertEqual(self.plan(rest), ("followup", "and Austin?", []))

    def test_rewritten_history_replays(self):
        final = {"role": "assistant", "content": "It is 71F."}
        self.conv.consumed = [transport.digest(m) for m in self.rest + [final]]
        edited = self.rest + [{"role": "assistant", "content": "[compressed summary]"},
                              {"role": "user", "content": "and Austin?"}]
        self.assertIsNone(self.plan(edited))

    def test_model_or_toolset_change_replays(self):
        self.conv.consumed = [transport.digest(self.rest[0])]
        rest = self.rest + [{"role": "user", "content": "more"}]
        self.assertIsNone(transport._plan(self.conv, [transport.digest(m) for m in rest], rest, ("other", ()), "sig"))
        self.assertIsNone(transport._plan(self.conv, [transport.digest(m) for m in rest], rest, ("m", ()), "new"))


class PromptTests(unittest.TestCase):
    def test_seed_prompt_wraps_system_and_renders_history(self):
        rest = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": None, "tool_calls": [call("a", "terminal", {"command": "ls"})]},
                {"role": "tool", "tool_call_id": "a", "content": "file.txt"},
                {"role": "assistant", "content": "One file."},
                {"role": "user", "content": [{"type": "text", "text": "what's in it?"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}]}]
        text, images = transport.build_seed_prompt("SYSTEM RULES", rest)
        self.assertIn("SYSTEM RULES", text)
        self.assertIn("<conversation_history>", text)
        self.assertIn("[assistant called tool terminal (id a)]", text)
        self.assertIn("[tool result (id a)]\nfile.txt", text)
        self.assertTrue(text.rstrip().endswith("what's in it?\n[image]"))
        self.assertEqual(images, [{"data": "QUJD", "mime_type": "image/png"}])

    def test_seed_prompt_after_tool_results_asks_to_continue(self):
        rest = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [call("a", "terminal", {})]},
                {"role": "tool", "tool_call_id": "a", "content": "done"}]
        text, _ = transport.build_seed_prompt("", rest)
        self.assertIn("Continue the conversation", text)

    def test_tool_schema_normalization(self):
        tools = transport.normalize_tools([{"type": "function", "function": {
            "name": "x", "description": "d", "parameters": {"anyOf": [{"type": "object"}]}}}])
        self.assertEqual(tools, [{"name": "x", "description": "d", "schema": {"type": "object", "properties": {}}}])


class CredentialTests(unittest.TestCase):
    def test_sidecar_env_never_carries_an_ambient_key(self):
        os.environ["CURSOR_API_KEY"] = "crsr_ambient"
        try:
            self.assertNotIn("CURSOR_API_KEY", rt.clean_env())
        finally:
            os.environ.pop("CURSOR_API_KEY")

    def test_client_without_hermes_credential_refuses(self):
        client = transport.Client(api_key="")
        with self.assertRaises(transport.CursorLoggedOut):
            client.chat.completions.create(model="composer-2.5", messages=[{"role": "user", "content": "hi"}])

    def test_rejected_key_is_401_with_reauth_advice(self):
        rejected = transport._error_from({"type": "error", "status": 401,
                                          "message": "AuthenticationError: unauthenticated: Invalid User API Key"})
        self.assertIsInstance(rejected, transport.CursorLoggedOut)
        self.assertEqual(rejected.status_code, 401)  # Hermes rotates to the next pooled key on 401
        self.assertIn("rejected the API key", str(rejected))
        missing = transport._error_from({"type": "error", "status": 401,
                                         "message": "AuthenticationError: Not signed in to Cursor: no credential"})
        self.assertIn("Not signed in", str(missing).split("(")[0])

    def test_no_plugin_code_reads_vendor_credential_files(self):
        for path in ROOT.rglob("*"):
            if path.suffix not in (".py", ".mjs", ".sh") and path.name != "cursor-sidecar":
                continue
            if ".venv" in path.parts or path.name == "test_offline.py":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("auth.json", text, path)
            self.assertNotIn(".cursor/sdk", text, path)


if __name__ == "__main__":
    unittest.main()
