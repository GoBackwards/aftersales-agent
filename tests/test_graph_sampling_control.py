"""Explicit sampling controls and actual-wire audit, with no provider access."""
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from support_agent import models
from support_agent.models import ToolDefinitions


SECRET = 'test-only-key-DO-NOT-LOG'
CONTENT = '仅本机消息秘密-DO-NOT-LOG'
TOOL_DESCRIPTION = '仅本机工具描述秘密-DO-NOT-LOG'
RESPONSE = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': '本机响应'},
                                   'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
                       'model': 'test-model'}, ensure_ascii=False).encode('utf-8')




def config(**extra):
    return models.APIConfig('https://example.invalid/v1', 'test-model', SECRET,
                            max_tokens=64, timeout=8, **extra)


def fake_model(**extra):
    adapter = models.ChatCompletionsModel(config(**extra))
    adapter.opener = Mock()
    adapter.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(RESPONSE)
    return adapter




class GraphSamplingConfigurationTests(unittest.TestCase):
    def test_omitted_and_blank_env_keep_temperature_unset(self):
        self.assertIsNone(config().temperature)
        base = {'LLM_BASE_URL': 'https://example.invalid/v1', 'LLM_MODEL': 'test-model',
                'LLM_API_KEY': SECRET}
        for extra in ({}, {'LLM_TEMPERATURE': '  '}):
            with self.subTest(extra=extra), patch.dict(os.environ, {**base, **extra}, clear=True):
                self.assertIsNone(models.APIConfig.from_env().temperature)

    def test_zero_and_finite_bounds_validate_without_coercing_booleans(self):
        for value in (0, 0.7, 2):
            with self.subTest(value=value):
                candidate = config(temperature=value)
                candidate.validate()
                self.assertEqual(candidate.temperature, value)
        env = {'LLM_BASE_URL': 'https://example.invalid/v1', 'LLM_MODEL': 'test-model',
               'LLM_API_KEY': SECRET, 'LLM_TEMPERATURE': ' 0 '}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(models.APIConfig.from_env().temperature, 0)

    def test_api_config_rejects_non_numeric_nonfinite_or_out_of_range(self):
        for value in (False, True, '0', float('nan'), float('inf'), -float('inf'), -0.01, 2.01):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config(temperature=value).validate()

    def test_env_rejects_invalid_temperature_before_any_request(self):
        base = {'LLM_BASE_URL': 'https://example.invalid/v1', 'LLM_MODEL': 'test-model',
                'LLM_API_KEY': SECRET}
        for value in ('true', 'nan', 'inf', '-inf', '-0.01', '2.01'):
            with self.subTest(value=value), patch.dict(os.environ, {**base, 'LLM_TEMPERATURE': value}, clear=True):
                with self.assertRaises(ValueError):
                    models.APIConfig.from_env()








class GraphSamplingWireTests(unittest.TestCase):
    def assert_safe_metadata(self, metadata, raw_body):
        body = json.loads(raw_body)
        self.assertEqual(set(metadata), {'body_sha256', 'body_canonical_sha256', 'parameters'})
        self.assertEqual(metadata['body_sha256'], hashlib.sha256(raw_body).hexdigest())
        canonical = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        self.assertEqual(metadata['body_canonical_sha256'], hashlib.sha256(canonical).hexdigest())
        self.assertNotEqual(metadata['body_sha256'], metadata['body_canonical_sha256'])
        expected = {key: body[key] for key in ('model', 'max_tokens', 'temperature', 'tool_choice',
                    'thinking', 'response_format', 'enable_thinking', 'parallel_tool_calls') if key in body}
        self.assertEqual(metadata['parameters'], expected)
        serialized = json.dumps(metadata, ensure_ascii=False)
        for forbidden in (SECRET, CONTENT, TOOL_DESCRIPTION, 'Authorization', 'headers', 'messages', 'tools'):
            self.assertNotIn(forbidden, serialized)

    def test_legacy_wire_omits_temperature_and_metadata_tracks_only_actual_fields(self):
        adapter = fake_model()
        adapter.complete([{'role': 'user', 'content': CONTENT}], [])
        raw = adapter.opener.open.call_args.args[0].data
        self.assertNotIn('temperature', json.loads(raw))
        self.assert_safe_metadata(adapter.last_request_metadata, raw)
        self.assertEqual(adapter.last_request_metadata['parameters'], {'model': 'test-model', 'max_tokens': 64})

    def test_zero_is_present_on_loopback_wire_with_exact_body_digest(self):
        captured = {}
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured['body'] = self.rfile.read(int(self.headers['Content-Length']))
                captured['authorization'] = self.headers.get('Authorization')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(RESPONSE)))
                self.end_headers()
                self.wfile.write(RESPONSE)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            adapter = models.ChatCompletionsModel(models.APIConfig(
                f'http://127.0.0.1:{server.server_port}/v1', 'test-model', SECRET,
                provider='deepseek', max_tokens=64, timeout=8, temperature=0), json_output=True)
            definitions = ToolDefinitions([{'type': 'function', 'function': {'name': 'local_probe',
                'description': TOOL_DESCRIPTION, 'parameters': {'type': 'object', 'properties': {}}}}],
                tool_choice={'type': 'function', 'function': {'name': 'local_probe'}})
            adapter.complete([{'role': 'user', 'content': CONTENT}], definitions)
            body = json.loads(captured['body'])
            self.assertEqual(body['temperature'], 0)
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertEqual(body['tool_choice'], {'type': 'function', 'function': {'name': 'local_probe'}})
            self.assertEqual(captured['authorization'], 'Bearer ' + SECRET)
            self.assert_safe_metadata(adapter.last_request_metadata, captured['body'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_qwen_flags_and_nonzero_temperature_match_actual_body(self):
        adapter = fake_model(provider='qwen', temperature=0.4)
        adapter.complete([{'role': 'user', 'content': CONTENT}], [])
        raw = adapter.opener.open.call_args.args[0].data
        self.assertEqual(json.loads(raw)['temperature'], 0.4)
        self.assertIs(json.loads(raw)['parallel_tool_calls'], False)
        self.assertIs(json.loads(raw)['enable_thinking'], False)
        self.assert_safe_metadata(adapter.last_request_metadata, raw)

    def test_http_error_records_this_attempt_body_and_preserves_previous_audit_copy(self):
        adapter = fake_model()
        adapter.complete([{'role': 'user', 'content': CONTENT}], [])
        first = deepcopy(adapter.last_request_metadata)
        adapter.opener.open.side_effect = HTTPError('https://example.invalid', 503, SECRET, {}, io.BytesIO())
        with self.assertRaises(models.ModelError):
            adapter.complete([{'role': 'user', 'content': CONTENT + '-different'}], [])
        raw = adapter.opener.open.call_args.args[0].data
        self.assert_safe_metadata(adapter.last_request_metadata, raw)
        self.assertNotEqual(first['body_sha256'], adapter.last_request_metadata['body_sha256'])
        self.assertEqual(first['parameters']['model'], 'test-model')
        self.assertNotIn(SECRET, json.dumps(adapter.last_request_metadata))

    def test_pre_body_failure_clears_old_metadata_and_does_not_claim_an_http_send(self):
        adapter = fake_model()
        adapter.complete([], [])
        for timeout in (0, float('nan')):
            with self.subTest(timeout=timeout), self.assertRaises(models.ModelError):
                adapter.complete_with_timeout([], [], timeout=timeout)
            self.assertIsNone(adapter.last_request_metadata)
        self.assertEqual(adapter.opener.open.call_count, 1)

    def test_unencodable_body_clears_metadata_before_serialization_failure(self):
        adapter = fake_model()
        adapter.complete([], [])
        with self.assertRaises(TypeError):
            adapter.complete([{'role': 'user', 'content': {object()}}], [])
        self.assertIsNone(adapter.last_request_metadata)
        self.assertEqual(adapter.opener.open.call_count, 1)




if __name__ == '__main__':
    unittest.main()
