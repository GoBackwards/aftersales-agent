from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from support_agent.models import APIConfig, ChatCompletionsModel, ModelError, load_dotenv


def api_payload(message=None, finish_reason="stop", **extra):
    if message is None:
        message = {"role": "assistant", "content": "你好"}
    return {"choices": [{"message": message, "finish_reason": finish_reason}], **extra}


def function_call(arguments, identifier="call-1", name="get_order"):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": arguments}}


class ChatCompletionsTests(unittest.TestCase):
    def make_model(self, payload, provider="compatible"):
        response = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        opener = Mock()
        opener.open.return_value = io.BytesIO(response)
        with patch("support_agent.models.urllib.request.build_opener", return_value=opener):
            model = ChatCompletionsModel(APIConfig(
                "https://example.invalid/v1", "test-model", "sk-test-only", provider=provider, timeout=12,
            ))
        return model, opener

    def test_provider_options_are_top_level_and_only_sent_to_that_provider(self):
        messages = [{"role": "user", "content": "查询 O1001"}]
        tools = [{"type": "function", "function": {"name": "get_order"}}]
        for provider in ("deepseek", "qwen", "compatible"):
            with self.subTest(provider=provider):
                model, opener = self.make_model(api_payload(), provider)
                model.complete(messages, tools)
                request = opener.open.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(request.full_url, "https://example.invalid/v1/chat/completions")
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer sk-test-only")
                self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
                self.assertEqual(body["messages"], messages)
                self.assertEqual(body["tools"], tools)
                self.assertNotIn("extra_body", body)
                self.assertNotIn("api_key", body)
                if provider == "deepseek":
                    self.assertEqual(body["thinking"], {"type": "disabled"})
                    self.assertNotIn("parallel_tool_calls", body)
                    self.assertNotIn("enable_thinking", body)
                elif provider == "qwen":
                    self.assertIs(body["enable_thinking"], False)
                    self.assertIs(body["parallel_tool_calls"], False)
                    self.assertNotIn("thinking", body)
                else:
                    self.assertFalse({"thinking", "enable_thinking", "parallel_tool_calls"} & body.keys())

    def test_multiple_calls_and_provider_history_are_not_reconstructed_or_lost(self):
        message = {
            "role": "assistant", "content": None, "reasoning_content": "原始字段\n不删改",
            "tool_calls": [function_call('{"order_id":"O1001"}'),
                           function_call('{"query":"退货"}', "call-2", "search_policy")],
        }
        original = deepcopy(message)
        model, _ = self.make_model(api_payload(message, "tool_calls", usage={"total_tokens": 42}))
        response = model.complete([], [])
        self.assertEqual(response.content, "")
        self.assertEqual([(call.id, call.name, call.arguments) for call in response.tool_calls], [
            ("call-1", "get_order", {"order_id": "O1001"}),
            ("call-2", "search_policy", {"query": "退货"}),
        ])
        self.assertEqual(response.raw_message, original)
        self.assertEqual(response.usage, {"total_tokens": 42})

    def test_no_tool_generation_omits_tool_parameters(self):
        model, opener = self.make_model(api_payload(), "deepseek")
        model.complete([{"role": "user", "content": "只读政策解释"}], [])
        body = json.loads(opener.open.call_args.args[0].data)
        self.assertNotIn("tools", body)
        self.assertNotIn("tool_choice", body)
        self.assertNotIn("response_format", body)
        self.assertEqual(body["thinking"], {"type": "disabled"})

    def test_json_generation_is_explicit_without_mutating_configuration(self):
        ordinary, opener = self.make_model(api_payload({"role": "assistant", "content": "{}"}), "deepseek")
        with patch("support_agent.models.urllib.request.build_opener", return_value=opener):
            model = ChatCompletionsModel(ordinary.config, json_output=True)
        model.complete([{"role": "system", "content": "Respond in JSON."}], [])
        body = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", body)
        self.assertFalse(ordinary.json_output)
        with self.assertRaises(ValueError):
            ChatCompletionsModel(ordinary.config, json_output="false")

    def test_malformed_json_or_structurally_invalid_response_is_a_model_error(self):
        duplicate = function_call("{}")
        invalid_payloads = [
            b"not json", b'{"choices": [', {}, {"choices": []}, {"choices": [None]},
            api_payload({"role": "tool", "content": "wrong role"}),
            api_payload({"role": "assistant", "content": ["not text"]}),
            api_payload({"role": "assistant", "tool_calls": [duplicate, duplicate]}),
            api_payload({"role": "assistant", "tool_calls": [function_call("{}", "")]}),
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                model, _ = self.make_model(payload)
                with self.assertRaises(ModelError):
                    model.complete([], [])

    def test_incomplete_messages_invalid_falsey_content_and_bad_usage_are_rejected(self):
        invalid_payloads = [api_payload({})]
        invalid_payloads += [api_payload({"role": "assistant", "content": content})
                             for content in (0, False, [], {})]
        invalid_payloads += [api_payload(usage=value) for value in ([1], "invalid usage", 7)]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                model, _ = self.make_model(payload)
                with self.assertRaises(ModelError):
                    model.complete([], [])

    def test_truncated_or_filtered_tool_calls_never_execute_even_with_valid_arguments(self):
        for reason in ("length", "content_filter"):
            with self.subTest(reason=reason):
                message = {"role": "assistant", "content": None, "tool_calls": [function_call(
                    '{"order_id":"O1001","reason":"不合适"}', name="create_return")],
                }
                model, _ = self.make_model(api_payload(message, reason))
                with self.assertRaises(ModelError):
                    model.complete([], [])

    def test_malformed_arguments_still_yield_one_tool_call_without_domain_validation(self):
        # Minimal inline schema; argument validation belongs to the caller, not the adapter.
        schemas = [{"type": "function", "function": {
            "name": "get_order", "description": "Read one simulated order.",
            "parameters": {"type": "object", "properties": {"order_id": {"type": "string", "minLength": 1}},
                           "required": ["order_id"], "additionalProperties": False}}}]
        for arguments in ('{"order_id":', "[]", "null", '{"order_id": 12}'):
            with self.subTest(arguments=arguments):
                message = {"role": "assistant", "content": None, "tool_calls": [function_call(arguments)]}
                model, _ = self.make_model(api_payload(message, "tool_calls"))
                response = model.complete([], schemas)
                self.assertEqual(len(response.tool_calls), 1)

    def test_http_and_connection_errors_do_not_disclose_url_or_response_secrets(self):
        secret = "sk-do-not-print"
        errors = [
            HTTPError(f"https://example.invalid/?key={secret}", 401, secret, {}, io.BytesIO(secret.encode())),
            URLError(f"connection failed: {secret}"),
            TimeoutError(secret),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                model, opener = self.make_model(api_payload())
                opener.open.side_effect = error
                with self.assertRaises(ModelError) as caught:
                    model.complete([], [])
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, caught.exception.public_message)


class ConfigurationTests(unittest.TestCase):
    def test_dotenv_loads_only_llm_fields_without_overriding_environment_or_printing(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"LLM_MODEL": "existing"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('\ufeff# local config\nLLM_API_KEY="sk-local-secret"\nLLM_MODEL=file-model\nPATH=ignored\n',
                            encoding="utf-8")
            with redirect_stdout(stdout), redirect_stderr(stderr):
                load_dotenv(path)
                load_dotenv(Path(directory) / "missing.env")
            self.assertEqual(os.environ["LLM_API_KEY"], "sk-local-secret")
            self.assertEqual(os.environ["LLM_MODEL"], "existing")
            self.assertNotIn("PATH", os.environ)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_malformed_dotenv_error_reports_line_number_without_the_secret(self):
        secret = "sk-invalid-line-secret"
        stdout, stderr = io.StringIO(), io.StringIO()
        with TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            path.write_text(f"# config\nLLM_API_KEY {secret}\n", encoding="utf-8")
            with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(ValueError) as caught:
                load_dotenv(path)
        self.assertIn("2", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception) + stdout.getvalue() + stderr.getvalue())

    def test_api_config_rejects_credentials_in_url_and_plain_remote_http(self):
        for base_url in ("http://api.example.invalid", "https://key:secret@example.invalid",
                         "https://example.invalid?key=secret", "https://example.invalid#secret"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError) as caught:
                APIConfig(base_url, "model", "sk-test").validate()
            self.assertNotIn("sk-test", str(caught.exception))
        APIConfig("http://127.0.0.1:8000/v1", "local-model", "local-placeholder").validate()


class AdapterRuntimeTests(unittest.TestCase):
    def make_model(self, payload=None):
        payload = payload if payload is not None else {"choices": [{"message": {
            "role": "assistant", "content": "完成"}}]}
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        opener = Mock()
        opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(body)
        with patch("support_agent.models.urllib.request.build_opener", return_value=opener):
            model = ChatCompletionsModel(APIConfig("https://example.invalid/v1", "test", "sk-test", timeout=12))
        return model, opener

    def test_model_error_old_constructor_and_invalid_metadata_defaults(self):
        error = ModelError("公开错误。")
        self.assertEqual(str(error), "公开错误。")
        self.assertEqual((error.retryable, error.status_code, error.retry_after), (False, None, None))
        error = ModelError("公开错误。", retryable="yes", status_code=True, retry_after=float("nan"))
        self.assertEqual((error.retryable, error.status_code, error.retry_after), (False, None, None))

    def test_only_transient_http_statuses_are_retryable_and_error_is_sanitized(self):
        for status in (400, 401, 403, 404, 408, 429, 500, 501, 502, 503, 504):
            with self.subTest(status=status):
                model, opener = self.make_model()
                opener.open.side_effect = HTTPError("https://invalid/?sk-secret", status, "sk-secret",
                                                   {"Retry-After": "1.25"}, io.BytesIO(b"sk-secret"))
                with self.assertRaises(ModelError) as caught:
                    model.complete([], [])
                error = caught.exception
                self.assertEqual(error.retryable, status in {429, 500, 502, 503, 504})
                self.assertEqual(error.status_code, status)
                self.assertEqual(error.retry_after, 1.25)
                self.assertNotIn("sk-secret", str(error))

    def test_connection_and_timeout_errors_are_retryable(self):
        for underlying in (URLError("sk-secret"), TimeoutError("sk-secret"), ConnectionError("sk-secret")):
            with self.subTest(error=type(underlying).__name__):
                model, opener = self.make_model()
                opener.open.side_effect = underlying
                with self.assertRaises(ModelError) as caught:
                    model.complete([], [])
                self.assertTrue(caught.exception.retryable)
                self.assertIsNone(caught.exception.status_code)
                self.assertNotIn("sk-secret", str(caught.exception))

    def test_malformed_response_is_nonretryable(self):
        for payload in (b"sk-secret-not-json", {"choices": []}, {"choices": [{"message": {"role": "tool"}}]}):
            with self.subTest(payload=payload):
                model, _ = self.make_model(payload)
                with self.assertRaises(ModelError) as caught:
                    model.complete([], [])
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn("sk-secret", str(caught.exception))

    def test_per_attempt_timeout_shortens_configuration_without_mutating_it(self):
        model, opener = self.make_model()
        model.complete_with_timeout([], [], timeout=0.25)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 0.25)
        model.complete_with_timeout([], [], timeout=99)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
        model.complete([], [])
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
        self.assertEqual(model.config.timeout, 12)

    def test_invalid_per_attempt_timeout_never_calls_opener(self):
        model, opener = self.make_model()
        for timeout in (0, -1, True, None, "1", float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ModelError):
                model.complete_with_timeout([], [], timeout=timeout)
        opener.open.assert_not_called()

    def test_retry_after_date_and_malformed_headers_are_parsed_without_leakage(self):
        for header, expected in (("Thu, 01 Jan 1970 00:01:45 GMT", 5), ("sk-secret", None),
                                 ("nan", None), ("-4", 0), (None, None)):
            with self.subTest(header=header):
                model, opener = self.make_model()
                headers = {} if header is None else {"Retry-After": header}
                opener.open.side_effect = HTTPError("https://invalid", 429, "error", headers, None)
                with patch("support_agent.models.time.time", return_value=100), self.assertRaises(ModelError) as caught:
                    model.complete([], [])
                self.assertEqual(caught.exception.retry_after, expected)
                self.assertNotIn("sk-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
