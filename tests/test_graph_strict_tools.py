"""Strict tool mode: schema transform, request body and the planner's (and, since B5a, the answer's) opt-in.

Strict mode asks the provider to constrain a function's arguments to its schema,
which stops the truncated or spliced argument JSON seen in about one in ten
DeepSeek planning calls. No request here reaches a real provider.
"""
from copy import deepcopy
import importlib.util
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from support_agent.models import APIConfig, ChatCompletionsModel, ToolDefinitions, strict_schema, strict_tool_definitions

GRAPH_AVAILABLE = importlib.util.find_spec("pydantic") is not None
CHOICE = {"type": "function", "function": {"name": "plan"}}


def tools(strict=("plan",)):
    return ToolDefinitions([
        {"type": "function", "function": {"name": "plan", "description": "d", "parameters": {
            "type": "object", "required": ["goal"], "additionalProperties": False, "properties": {
                "goal": {"type": "string", "minLength": 1, "maxLength": 600, "title": "Goal"},
                "title": {"type": "string", "default": ""},
                "items": {"type": "array", "maxItems": 2, "default": [], "items": {
                    "type": "object", "properties": {"id": {"type": "string", "pattern": "^q[1-6]$"},
                                                     "n": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]}}}},
                "kind": {"enum": ["a", "b"], "type": "string", "default": "a"}}}}}],
        tool_choice=CHOICE, strict_functions=strict)


class StrictSchemaTests(unittest.TestCase):
    def test_every_object_requires_all_properties_and_unsupported_keywords_are_dropped(self):
        schema = strict_schema(tools()[0]["function"]["parameters"])
        self.assertEqual(schema["required"], ["goal", "title", "items", "kind"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["goal"], {"type": "string"})
        self.assertIn("title", schema["properties"], "a property named title is data, not a keyword")
        item = schema["properties"]["items"]
        self.assertNotIn("maxItems", item)
        self.assertEqual(item["items"]["required"], ["id", "n"])
        self.assertIs(item["items"]["additionalProperties"], False)
        self.assertEqual(item["items"]["properties"]["id"]["pattern"], "^q[1-6]$")
        self.assertEqual(item["items"]["properties"]["n"]["anyOf"][0]["minimum"], 0)
        self.assertEqual(schema["properties"]["kind"], {"enum": ["a", "b"], "type": "string"})

    def test_only_opted_in_functions_change_and_inputs_stay_untouched(self):
        original = tools()
        before = deepcopy(list(original))
        sent = strict_tool_definitions(original)
        self.assertIs(sent[0]["function"]["strict"], True)
        self.assertEqual(list(original), before)
        self.assertNotIn("strict", strict_tool_definitions(tools(strict=()))[0]["function"])
        with self.assertRaises(ValueError):
            ToolDefinitions([{"type": "function", "function": {"name": "plan"}}], strict_functions={"other"})
        self.assertEqual(deepcopy(original).strict_functions, frozenset({"plan"}))


class StrictRequestTests(unittest.TestCase):
    def body(self, *, strict_tools, base_url="https://api.deepseek.com/beta", definitions=None):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "plan", "arguments": "{\"goal\": \"x\"}"}}]}}]}).encode())
        with patch("support_agent.models.urllib.request.build_opener", return_value=opener):
            model = ChatCompletionsModel(APIConfig(base_url, "deepseek-flash", "sk-test-only", provider="deepseek",
                                                   strict_tools=strict_tools))
        model.complete([{"role": "user", "content": "hi"}], definitions if definitions is not None else tools())
        return json.loads(opener.open.call_args.args[0].data), model.last_request_metadata

    def test_strict_functions_are_sent_strict_only_when_the_adapter_enables_it(self):
        body, metadata = self.body(strict_tools=True)
        self.assertIs(body["tools"][0]["function"]["strict"], True)
        self.assertEqual(body["tools"][0]["function"]["parameters"]["required"], ["goal", "title", "items", "kind"])
        self.assertEqual(body["tool_choice"], CHOICE)
        self.assertEqual(metadata["parameters"]["strict_functions"], ["plan"])
        body, metadata = self.body(strict_tools=False, base_url="https://api.deepseek.com")
        self.assertNotIn("strict", body["tools"][0]["function"])
        self.assertNotIn("strict_functions", metadata["parameters"])
        body, _ = self.body(strict_tools=True, definitions=tools(strict=()))
        self.assertNotIn("strict", body["tools"][0]["function"])

    def test_deepseek_strict_mode_requires_the_beta_endpoint_and_env_opt_in(self):
        with self.assertRaisesRegex(ValueError, "beta"):
            APIConfig("https://api.deepseek.com", "deepseek-flash", "sk-test-only", provider="deepseek",
                      strict_tools=True).validate()
        APIConfig("https://api.deepseek.com/beta", "deepseek-flash", "sk-test-only", provider="deepseek",
                  strict_tools=True).validate()
        env = {"LLM_BASE_URL": "https://api.deepseek.com/beta", "LLM_MODEL": "deepseek-flash",
               "LLM_API_KEY": "sk-test-only", "LLM_PROVIDER": "deepseek", "LLM_STRICT_TOOLS": "1"}
        with patch.dict(os.environ, env, clear=True):
            self.assertIs(APIConfig.from_env().strict_tools, True)
        with patch.dict(os.environ, {**env, "LLM_STRICT_TOOLS": ""}, clear=True):
            self.assertIs(APIConfig.from_env().strict_tools, False)
        with patch.dict(os.environ, {**env, "LLM_STRICT_TOOLS": "enabled"}, clear=True), \
                self.assertRaisesRegex(ValueError, "LLM_STRICT_TOOLS"):
            APIConfig.from_env()



@unittest.skipUnless(GRAPH_AVAILABLE, "install the optional graph dependencies")
class PlannerOptInTests(unittest.TestCase):
    def test_the_planning_and_answer_calls_both_opt_into_strict_mode(self):
        import http.client
        from pathlib import Path
        import tempfile
        import threading
        from tests.test_graph_agent import ScriptedFactory, ScriptedModel, answered, planned
        from support_agent.service import create_server

        class Recording(ScriptedModel):
            def complete_with_timeout(self, messages, tools, *, timeout):
                self.seen.append(getattr(tools, "strict_functions", None))
                return super().complete_with_timeout(messages, tools, timeout=timeout)

        model = Recording([planned(), answered("好的。")])
        model.seen = []
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(db_path=Path(directory) / "g.sqlite3", credentials={"t": "alice"}, port=0,
                                   model_factory=ScriptedFactory(model),
                                   log_path=Path(directory) / "r.jsonl")
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                def call(path, body):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
                    connection.request("POST", path, json.dumps(body, ensure_ascii=False).encode(),
                                       {"Authorization": "Bearer t", "Content-Type": "application/json"})
                    data = json.loads(connection.getresponse().read())
                    connection.close()
                    return data
                session = call("/sessions", {})["session_id"]
                self.assertEqual(call(f"/sessions/{session}/messages",
                                      {"message": "你好", "request_id": "m1"})["status"], "completed")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        # B5a: the answer call opts in too (it used to send an empty set); B5d2 names it respond.
        self.assertEqual(model.seen, [frozenset({"plan_turn"}), frozenset({"respond"})])

if __name__ == "__main__":
    unittest.main()
