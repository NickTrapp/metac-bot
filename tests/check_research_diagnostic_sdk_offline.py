"""Locked OpenAI SDK preflight with MockTransport and all sockets forbidden."""
import copy
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
logging.disable(logging.CRITICAL)


def deny_network(*args, **kwargs):
    raise AssertionError("Network forbidden in diagnostic SDK preflight")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

import httpx
import openai
import research_diagnostic_check as check
import prospective_forecast as pf
from test_prospective_forecast import NOW
from test_research_diagnostic import apollo_response

SECRET = "SYNTHETIC_UNSAFE_RESPONSE_MESSAGE_DO_NOT_PERSIST"


class DiagnosticSDKTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, status, body):
        root = Path(__file__).resolve().parents[1]
        lock = tomllib.loads((root / "poetry.lock").read_text())
        expected = next(p["version"] for p in lock["package"] if p["name"] == "openai")
        self.assertEqual(openai.__version__, expected, "Use the existing lockfile")
        requests, snapshots = [], []
        payload = pf.research_request(check.QUESTION, pf.question_input(check.QUESTION, NOW.isoformat()))

        def handler(request):
            requests.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            self.assertEqual(request.method, "POST")
            self.assertEqual(requests[-1], payload)
            return httpx.Response(status, json=body)

        async with openai.AsyncOpenAI(api_key="offline-placeholder", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            record = await check.run_probe(client, check.new_record(),
                checkpoint=lambda r: snapshots.append(copy.deepcopy(r)), clock=lambda: NOW)
        self.assertEqual(len(requests), 1)
        self.assertEqual(record["request_attempts"], 1)
        self.assertEqual(payload["tools"][0]["filters"], {"allowed_domains": ["nasa.gov"]})
        self.assertFalse(payload["store"])
        self.assertEqual(payload["max_tool_calls"], 2)
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertNotIn(SECRET, json.dumps(snapshots))
        return record

    async def test_success_uses_exact_prospective_payload(self):
        value = apollo_response()
        value.update(object="response", created_at=1)
        record = await self.probe(200, value)
        self.assertEqual(record["operational_status"], "completed")
        self.assertTrue(record["sources_validated"])
        self.assertTrue(record["usage_complete"])

    async def test_actual_sdk_400_only_allowlisted_metadata(self):
        record = await self.probe(400, {"error": {"message": SECRET,
            "type": "invalid_request_error", "code": "unsupported_parameter", "param": SECRET}})
        self.assertEqual(record["failure_diagnostic"], {
            "phase": "sdk_request", "response_received": False,
            "exception_class": "BadRequestError", "http_status": 400,
            "provider_code": "unsupported_parameter"})
        self.assertIsNone(record["estimated_cost_usd"])
        self.assertFalse(record["usage_complete"])

    async def test_actual_sdk_429_does_not_retry(self):
        record = await self.probe(429, {"error": {"message": SECRET,
            "type": "rate_limit_error", "code": "rate_limit_exceeded"}})
        self.assertEqual(record["failure_diagnostic"]["exception_class"], "RateLimitError")
        self.assertEqual(record["failure_diagnostic"]["http_status"], 429)

    async def test_actual_sdk_unknown_code_is_not_retained(self):
        record = await self.probe(400, {"error": {"message": SECRET,
            "type": "invalid_request_error", "code": SECRET}})
        self.assertIsNone(record["failure_diagnostic"]["provider_code"])


if __name__ == "__main__":
    unittest.main()
