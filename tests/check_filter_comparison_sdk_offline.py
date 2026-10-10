"""Locked SDK control-pair serialization and errors, with all sockets blocked."""
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
    raise AssertionError("Network forbidden in comparison SDK preflight")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

import httpx
import openai
import research_filter_comparison as check
from test_prospective_forecast import NOW
from test_filter_comparison import apollo_response

SECRET = "SYNTHETIC_UNSAFE_ERROR_MESSAGE_DO_NOT_PERSIST"


def error_body(param, code="unsupported_parameter"):
    return {"error": {"message": SECRET, "type": "invalid_request_error", "code": code, "param": param}}


class ComparisonSDKTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, responses):
        root = Path(__file__).resolve().parents[1]
        lock = tomllib.loads((root / "poetry.lock").read_text())
        expected = next(p["version"] for p in lock["package"] if p["name"] == "openai")
        self.assertEqual(openai.__version__, expected)
        requests, snapshots = [], []
        payloads = check.build_payloads(NOW.isoformat())

        def handler(request):
            index = len(requests)
            self.assertLess(index, 2, "No SDK retry or additional control")
            requests.append(json.loads(request.content))
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            self.assertEqual(request.method, "POST")
            self.assertEqual(requests[-1], payloads[index][1])
            status, body = responses[index]
            return httpx.Response(status, json=body)

        async with openai.AsyncOpenAI(api_key="offline-placeholder", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            record = await check.run_comparison(client, check.new_record(),
                checkpoint=lambda r: snapshots.append(copy.deepcopy(r)), clock=lambda: NOW)
        self.assertEqual(len(requests), 2)
        self.assertEqual(record["request_attempts"], 2)
        control = copy.deepcopy(requests[0])
        self.assertEqual(control["tools"][0].pop("filters"), {"allowed_domains": ["nasa.gov"]})
        self.assertEqual(control, requests[1])
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertNotIn(SECRET, json.dumps(snapshots))
        return record

    async def test_actual_sdk_parameter_error_and_successful_control(self):
        record = await self.probe([(400, error_body("tools[0].filters")), (200, apollo_response())])
        self.assertEqual(record["requests"][0]["failure_diagnostic"]["provider_param"], "tools[0].filters")
        self.assertEqual(record["comparison_outcome"], "filtered_rejected_400_unfiltered_returned")
        self.assertIsNone(record["total_estimated_cost_usd"])

    async def test_actual_sdk_same_rejected_parameter(self):
        record = await self.probe([(400, error_body("max_tool_calls"))] * 2)
        self.assertEqual(record["comparison_outcome"], "both_rejected_400_same_parameter")

    async def test_rate_limit_never_adds_sdk_retry(self):
        record = await self.probe([(429, error_body(None, "rate_limit_exceeded")), (200, apollo_response())])
        self.assertEqual(record["requests"][0]["failure_diagnostic"]["exception_class"], "RateLimitError")
        self.assertEqual(record["comparison_outcome"], "inconclusive")

    async def test_unknown_error_parameter_is_null(self):
        record = await self.probe([(400, error_body(SECRET, SECRET)), (200, apollo_response())])
        error = record["requests"][0]["failure_diagnostic"]
        self.assertIsNone(error["provider_param"])
        self.assertIsNone(error["provider_code"])


if __name__ == "__main__":
    unittest.main()
