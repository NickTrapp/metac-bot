"""Exact locked SDK, offline two-stage transport and safe failure handling."""
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
    raise AssertionError("Network forbidden in compatibility SDK preflight")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

import httpx
import openai
import research_compatibility_check as check
import research_compatibility_v1 as compatibility
from test_prospective_forecast import NOW
from test_compatibility_check import apollo_response, factual_response

SECRET = "SYNTHETIC_UNSAFE_PROVIDER_MESSAGE_DO_NOT_PERSIST"


class CompatibilitySDKTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, responses):
        root = Path(__file__).resolve().parents[1]
        lock = tomllib.loads((root / "poetry.lock").read_text())
        self.assertEqual(openai.__version__, next(p["version"] for p in lock["package"] if p["name"] == "openai"))
        requests, snapshots = [], []
        data = check.pf.question_input(check.QUESTION, NOW.isoformat())

        def handler(request):
            index = len(requests)
            self.assertLess(index, len(responses), "No retries or unexpected requests")
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            self.assertEqual(request.method, "POST")
            self.assertEqual(payload["model"], compatibility.PINNED_MODEL)
            self.assertFalse(payload["store"])
            self.assertNotIn("previous_response_id", payload)
            self.assertNotIn("conversation", payload)
            if index == 0:
                self.assertEqual(payload, compatibility.research_request(check.QUESTION, data))
                self.assertNotIn("filters", payload["tools"][0])
                self.assertEqual(payload["max_tool_calls"], 2)
            else:
                self.assertEqual(payload["tools"], [])
                self.assertEqual(payload["tool_choice"], "none")
                self.assertNotIn("max_tool_calls", payload)
                self.assertEqual(payload["text"]["format"]["schema"], compatibility.ANSWER_SCHEMA)
                self.assertEqual(payload["instructions"], compatibility.ANSWER_INSTRUCTIONS)
                self.assertEqual(json.loads(payload["input"])["as_of_utc"], NOW.isoformat())
            status, body = responses[index]
            return httpx.Response(status, json=body)

        async with openai.AsyncOpenAI(api_key="offline-placeholder", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            record = await check.run_pipeline(client, check.new_record(),
                checkpoint=lambda r: snapshots.append(copy.deepcopy(r)), clock=lambda: NOW)
        self.assertEqual(len(requests), len(responses))
        self.assertEqual(record["batch_request_attempts"], 2 + len(responses))
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertNotIn(SECRET, json.dumps(snapshots))
        return record

    async def test_corrected_research_and_structured_answer_both_succeed(self):
        record = await self.probe([(200, apollo_response()), (200, factual_response())])
        self.assertEqual(record["operational_status"], "completed")
        self.assertEqual(record["factual_answer"]["answer"], "yes")
        self.assertTrue(record["known_fact_agrees"])
        self.assertTrue(record["usage_complete"])
        self.assertFalse(record["market_blind_research"])

    async def test_research_rejection_does_not_send_answer(self):
        record = await self.probe([(400, {"error": {"message": SECRET,
            "type": "invalid_request_error", "code": "unsupported_parameter", "param": "tools"}})])
        self.assertEqual(record["requests"][0]["failure_diagnostic"]["provider_param"], "tools")
        self.assertEqual(record["operational_status"], "failed")

    async def test_answer_rate_limit_has_no_retry_and_unknown_total(self):
        record = await self.probe([(200, apollo_response()), (429, {"error": {"message": SECRET,
            "type": "rate_limit_error", "code": "rate_limit_exceeded", "param": None}})])
        self.assertEqual(record["requests"][1]["failure_diagnostic"]["exception_class"], "RateLimitError")
        self.assertIsNone(record["total_estimated_cost_usd"])
        self.assertIsNotNone(record["source_provenance"])

    async def test_uncited_answer_source_rejected_without_repair_call(self):
        record = await self.probe([(200, apollo_response()),
                                  (200, factual_response(url="https://nasa.gov/uncited"))])
        self.assertEqual(record["pipeline_failure_diagnostic"]["phase"], "answer_parse")
        self.assertEqual(record["operational_status"], "failed")
        self.assertTrue(record["usage_complete"])


if __name__ == "__main__":
    unittest.main()
