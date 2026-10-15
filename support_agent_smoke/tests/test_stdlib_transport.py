import json
import os
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from supportlab.stdlib_transport import StdlibResponsesBackend, TransportError, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
from supportlab.llm import OpenAIResponsesBackend


class Response:
    def __init__(self, payload=None, status=200, content_type="application/json", raw=None):
        self.status = status
        self.content_type = content_type
        self.raw = raw if raw is not None else json.dumps(payload).encode()
        self.reads = 0
    def getheader(self, key, default=None):
        return self.content_type
    def read(self, size):
        self.reads += 1
        return self.raw[:size]


class Connection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False
    def request(self, method, path, body, headers):
        self.requests.append((method, path, json.loads(body), headers))
    def getresponse(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response
    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-secret", "HTTPS_PROXY": "http://untrusted.invalid", "OPENAI_BASE_URL": "https://untrusted.invalid"})
        self.env.start()
        self.socket = patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        self.socket.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.socket.stop)

    def backend(self, response):
        connection = Connection(response)
        calls = []
        def factory(host, **options):
            calls.append((host, options))
            return connection
        return StdlibResponsesBackend(connection_factory=factory), connection, calls

    def test_count_exact_contract_fixed_host_and_endpoint(self):
        backend, connection, calls = self.backend(Response({"input_tokens": 42}))
        payload = {"model": "snapshot", "input": "hi", "instructions": "rules", "tools": [], "tool_choice": "required", "max_output_tokens": 8192, "reasoning": {"effort": "xhigh"}, "parallel_tool_calls": False, "store": False}
        self.assertEqual(backend.count_input_tokens(payload), 42)
        method, path, body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/responses/input_tokens"))
        self.assertEqual(set(body), {"model", "input", "instructions", "tools", "tool_choice", "reasoning", "parallel_tool_calls"})
        self.assertEqual(body["reasoning"], {"effort": "xhigh"})
        self.assertIs(body["parallel_tool_calls"], False)
        self.assertEqual(calls[0][0], "api.openai.com")
        self.assertEqual(calls[0][1]["timeout"], 120)
        self.assertEqual(headers["Authorization"], "Bearer synthetic-secret")
        self.assertTrue(connection.closed)

    def test_generation_payload_unchanged_and_reasoning_removed(self):
        payload = {"model": "snapshot", "input": "hi", "tools": [], "store": False, "reasoning": {"effort": "xhigh"}, "max_output_tokens": 8192}
        response = {"id": "response-1", "model": "snapshot", "status": "completed", "output": [{"type": "reasoning", "summary": "not exported"}, {"type": "function_call", "name": "answer", "arguments": "{}", "call_id": "call-1", "ignored": "extra"}], "usage": {"input_tokens": 10, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 2}, "output_tokens_details": {"reasoning_tokens": 15}}, "private_extra": "not exported"}
        backend, connection, _ = self.backend(Response(response))
        actual = backend.request(payload)
        self.assertEqual(connection.requests[0][2], payload)
        self.assertEqual(connection.requests[0][1], "/v1/responses")
        self.assertEqual(actual, {"id": "response-1", "model": "snapshot", "status": "completed", "output": [{"type": "function_call", "name": "answer", "arguments": "{}", "call_id": "call-1"}], "usage": response["usage"]})

    def test_all_supported_input_fields_match_sdk_preflight(self):
        payload = {"model":"snapshot", "input":"hi", "instructions":"rules", "tools":[],
                   "tool_choice":"required", "parallel_tool_calls":False,
                   "reasoning":{"effort":"xhigh"}, "text":{"format":{"type":"text"}},
                   "conversation":None, "previous_response_id":None, "truncation":"disabled",
                   "personality":"pragmatic", "max_output_tokens":8192, "store":False}
        expected = {k:v for k,v in payload.items() if k not in {"max_output_tokens", "store"}}
        sdk_calls=[]
        def count(**kwargs):
            sdk_calls.append(kwargs)
            return SimpleNamespace(input_tokens=42)
        client=SimpleNamespace(responses=SimpleNamespace(input_tokens=SimpleNamespace(count=count)))
        sdk=OpenAIResponsesBackend(client=client,timeout_seconds=120)
        backend,connection,_=self.backend(Response({"input_tokens":42}))
        self.assertEqual(backend.count_input_tokens(payload),sdk.count_input_tokens(payload))
        sdk_payload=dict(sdk_calls[0]);self.assertEqual(sdk_payload.pop("timeout"),120)
        self.assertEqual(connection.requests[0][2],expected)
        self.assertEqual(sdk_payload,expected)
        self.assertEqual(payload["max_output_tokens"],8192)
        self.assertIs(payload["store"],False)

    def test_projection_and_payload_match_original_sdk_backend(self):
        raw = {"id": "response-parity", "model": "snapshot", "status": "completed",
               "output": [{"type": "reasoning", "summary": []}, {"type": "function_call", "name": "answer", "arguments": "{}", "call_id": "call-1"}],
               "usage": {"input_tokens": 42, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 5}, "output_tokens_details": {"reasoning_tokens": 10}}}
        def namespace(value):
            if isinstance(value, dict): return SimpleNamespace(**{k:namespace(v) for k,v in value.items()})
            if isinstance(value, list): return [namespace(v) for v in value]
            return value
        sdk_calls = []
        def create(**kwargs):
            sdk_calls.append(kwargs)
            return namespace(raw)
        client = SimpleNamespace(responses=SimpleNamespace(create=create, input_tokens=SimpleNamespace(count=lambda **kwargs:SimpleNamespace(input_tokens=42))))
        sdk = OpenAIResponsesBackend(client=client, timeout_seconds=120)
        backend, connection, _ = self.backend(Response(raw))
        payload = {"model":"snapshot", "input":"hi", "instructions":"rules", "tools":[], "tool_choice":"required", "parallel_tool_calls":False, "max_output_tokens":8192, "store":False, "reasoning":{"effort":"xhigh"}}
        self.assertEqual(backend.request(payload), sdk.request(payload))
        sdk_payload = dict(sdk_calls[0]); sdk_payload.pop("timeout")
        self.assertEqual(connection.requests[0][2], sdk_payload)

    def test_redirect_and_http_errors_never_follow_or_read_body(self):
        for status in (301, 302, 307, 400, 401, 429, 500):
            with self.subTest(status=status):
                response = Response(status=status, raw=b"secret-bearing error body")
                backend, connection, calls = self.backend(response)
                with self.assertRaisesRegex(TransportError, "^provider_http_failure$"):
                    backend.count_input_tokens({"model": "snapshot"})
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(connection.requests), 1)
                self.assertEqual(response.reads, 0)
                self.assertTrue(connection.closed)

    def test_network_error_no_retry_and_no_error_details(self):
        backend, connection, calls = self.backend(TimeoutError("do not expose private exception"))
        with self.assertRaisesRegex(TransportError, "^provider_transport_failed$"):
            backend.request({"model": "snapshot"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(connection.requests), 1)

    def test_non_json_and_oversize_fail(self):
        for response in (Response(raw=b"broken"), Response(raw=b"NaN"), Response([]), Response({}, content_type="text/html"), Response(raw=b"x" * (MAX_RESPONSE_BYTES+1))):
            with self.subTest(response=response):
                backend, _, _ = self.backend(response)
                with self.assertRaises(TransportError):
                    backend.count_input_tokens({"model": "snapshot"})

    def test_token_count_is_exact_nonnegative_integer(self):
        for tokens in (None, -1, True, 1.5, "4"):
            backend, _, _ = self.backend(Response({"input_tokens": tokens}))
            with self.assertRaisesRegex(TransportError, "provider_token_count_invalid"):
                backend.count_input_tokens({"model": "snapshot"})

    def test_missing_secret_and_oversize_never_connect(self):
        backend, _, calls = self.backend(Response({}))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(TransportError, "credential_unavailable"):
                backend.count_input_tokens({"model": "snapshot"})
        with self.assertRaisesRegex(TransportError, "request_too_large"):
            backend.count_input_tokens({"input": "x" * MAX_REQUEST_BYTES})
        self.assertEqual(calls, [])

    def test_bad_provider_output_and_usage_fail(self):
        for response in ({"output": None}, {"output": ["bad"]}, {"output": [], "usage": "bad"}, {"output": [], "usage": {"input_tokens_details": "bad"}}):
            backend, _, _ = self.backend(Response(response))
            with self.assertRaises(TransportError):
                backend.request({"model": "snapshot"})


if __name__ == "__main__":
    unittest.main()
