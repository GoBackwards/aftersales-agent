"""Request-option propagation and provider payloads, using zero-network substitutes."""

from copy import deepcopy
import io
import json
import unittest
from unittest.mock import Mock, patch

from support_agent.models import APIConfig, ChatCompletionsModel, ToolDefinitions, requested_tool_choice


def schemas():
    return [{"type": "function", "function": {"name": "compose_policy_answer",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
            {"type": "function", "function": {"name": "search_public_policy",
            "parameters": {"type": "object", "properties": {}}}}]


def named_choice(name="compose_policy_answer"):
    return {"type": "function", "function": {"name": name}}


class ToolDefinitionsTests(unittest.TestCase):
    def test_ordinary_lists_retain_auto_and_no_tools_omit_choice(self):
        self.assertEqual(requested_tool_choice(schemas()), "auto")
        self.assertIsNone(requested_tool_choice([]))
        self.assertIsNone(requested_tool_choice(ToolDefinitions()))

    def test_explicit_standard_choices_are_validated(self):
        for choice in ("auto", "none", "required", named_choice()):
            with self.subTest(choice=choice):
                self.assertEqual(requested_tool_choice(ToolDefinitions(schemas(), tool_choice=choice)), choice)
        with self.assertRaises(ValueError):
            ToolDefinitions([], tool_choice="required")

    def test_unknown_or_malformed_choices_are_rejected(self):
        choices = [None, True, 1, "force", "REQUIRED", {}, {"name": "compose_policy_answer"},
                   {"type": "function", "function": "compose_policy_answer"},
                   {"type": "function", "function": {"name": "compose_policy_answer", "extra": 1}},
                   {**named_choice(), "extra": 1}, {**named_choice(), "type": "custom"}]
        for choice in choices:
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                ToolDefinitions(schemas(), tool_choice=choice)

    def test_named_tool_must_exist_once(self):
        for definitions, name in ((schemas(), "absent"), (schemas(), ""), (schemas(), None),
                                  ([], "compose_policy_answer"),
                                  (schemas() + schemas()[:1], "compose_policy_answer")):
            with self.subTest(name=name, definitions=definitions), self.assertRaises(ValueError):
                ToolDefinitions(definitions, tool_choice=named_choice(name))

    def test_choice_cannot_be_changed_through_caller_or_returned_dictionary(self):
        choice = named_choice()
        definitions = ToolDefinitions(schemas(), tool_choice=choice)
        choice["function"]["name"] = "absent"
        returned = definitions.tool_choice
        returned["function"]["name"] = "absent"
        self.assertEqual(definitions.tool_choice, named_choice())

    def test_mutated_schema_list_is_revalidated_before_use(self):
        definitions = ToolDefinitions(schemas(), tool_choice=named_choice())
        definitions.pop(0)
        with self.assertRaises(ValueError):
            requested_tool_choice(definitions)
        required = ToolDefinitions(schemas(), tool_choice="required")
        required.clear()
        with self.assertRaises(ValueError):
            requested_tool_choice(required)

    def test_deepcopy_retains_choice_and_json_retains_only_schema_array(self):
        definitions = ToolDefinitions(schemas(), tool_choice=named_choice())
        copied = deepcopy(definitions)
        self.assertIsInstance(copied, ToolDefinitions)
        self.assertEqual(copied.tool_choice, named_choice())
        self.assertEqual(json.loads(json.dumps(copied)), schemas())
        copied[0]["function"]["parameters"]["description"] = "copied only"
        self.assertNotIn("description", definitions[0]["function"]["parameters"])


class ModelRequestContractTests(unittest.TestCase):
    def make_model(self, provider="deepseek"):
        payload = {"model": "fixture-model", "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "compose_policy_answer", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
        opener = Mock()
        opener.open.side_effect = lambda *_args, **_kwargs: io.BytesIO(json.dumps(payload).encode())
        with patch("support_agent.models.urllib.request.build_opener", return_value=opener):
            model = ChatCompletionsModel(APIConfig("https://example.invalid/v1", "fixture-model", "unit-test-key",
                                                   provider=provider, max_tokens=2048, timeout=45))
        return model, opener

    def test_deepseek_required_and_named_choices_reach_real_adapter_http_payload(self):
        for choice in ("required", named_choice()):
            with self.subTest(choice=choice):
                model, opener = self.make_model()
                definitions = ToolDefinitions(schemas(), tool_choice=choice)
                model.complete_with_timeout([{"role": "user", "content": "policy"}], definitions, timeout=7)
                request = opener.open.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(body["tool_choice"], choice)
                self.assertEqual(body["tools"], schemas())
                self.assertEqual(body["thinking"], {"type": "disabled"})
                self.assertEqual(body["max_tokens"], 2048)
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 7)
                self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
                self.assertNotIn("strict", body)
                self.assertTrue(all("strict" not in item["function"] for item in body["tools"]))
                self.assertNotIn("extra_body", body)

    def test_plain_lists_still_send_auto_for_every_provider(self):
        for provider in ("compatible", "qwen", "deepseek"):
            with self.subTest(provider=provider):
                model, opener = self.make_model(provider)
                model.complete([], schemas())
                self.assertEqual(json.loads(opener.open.call_args.args[0].data)["tool_choice"], "auto")

    def test_empty_tools_omit_tool_fields_even_with_explicit_empty_contract(self):
        for definitions in ([], ToolDefinitions(), ToolDefinitions(tool_choice="none")):
            with self.subTest(definitions=type(definitions).__name__):
                model, opener = self.make_model()
                model.complete([], definitions)
                body = json.loads(opener.open.call_args.args[0].data)
                self.assertNotIn("tools", body)
                self.assertNotIn("tool_choice", body)

    def test_schema_mutation_is_rejected_before_http_request(self):
        model, opener = self.make_model()
        definitions = ToolDefinitions(schemas(), tool_choice=named_choice())
        definitions.clear()
        with self.assertRaises(ValueError):
            model.complete([], definitions)
        opener.open.assert_not_called()




if __name__ == "__main__":
    unittest.main()
