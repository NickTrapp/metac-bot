"""Real locked SDK through MockTransport, all network sockets forbidden."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"


def deny_network(*args, **kwargs):
    raise AssertionError("Network forbidden in prospective SDK check")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

import httpx
import openai
import prospective_forecast as pf
from test_prospective_forecast import manifest, NOW, SOURCE


class ProspectiveSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_sdk_no_search_and_primary_domain_search(self):
        root = Path(__file__).resolve().parents[1]
        lock = tomllib.loads((root / "poetry.lock").read_text())
        expected = next(p["version"] for p in lock["package"] if p["name"] == "openai")
        self.assertEqual(openai.__version__, expected, "Use the existing lockfile")
        requests = []

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            self.assertEqual(request.method, "POST")
            stage = (len(requests) - 1) % 3
            self.assertEqual(payload["model"], pf.rf.MODEL)
            self.assertEqual(payload["max_output_tokens"], 3000)
            self.assertFalse(payload["store"])
            self.assertNotIn("previous_response_id", payload)
            self.assertNotIn("conversation", payload)
            if stage == 1:
                self.assertEqual(payload["max_tool_calls"], 2)
                self.assertEqual(payload["tool_choice"], "required")
                self.assertEqual(payload["tools"][0]["filters"], {"allowed_domains": ["example.org"]})
                text = "Synthetic fair coin evidence."
                annotations = [{"type": "url_citation", "url": SOURCE, "title": "Fixture",
                                "start_index": 0, "end_index": len(text)}]
                search = [{"id": "ws_fake", "type": "web_search_call", "status": "completed",
                           "action": {"type": "search", "query": "synthetic fair coin"}}]
            else:
                self.assertEqual(payload["instructions"], pf.FORECAST_INSTRUCTIONS)
                self.assertEqual(payload["tools"], [])
                self.assertEqual(payload["tool_choice"], "none")
                text = json.dumps({"probability_yes": .5, "rationale": "Synthetic fair coin",
                                   "uncertainties": [], "source_urls": [] if stage == 0 else [SOURCE]})
                search, annotations = [], []
            return httpx.Response(200, json={"id": f"resp_{len(requests)}", "object": "response", "created_at": 1,
                "status": "completed", "model": pf.rf.MODEL, "error": None, "incomplete_details": None,
                "output": search + [{"id": "msg_fake", "type": "message", "role": "assistant",
                                     "status": "completed", "content": [{"type": "output_text", "text": text,
                                                                            "annotations": annotations}]}],
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}})

        reserved = []
        async with openai.AsyncOpenAI(api_key="offline-placeholder", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            record = await pf.run_pilot(manifest(), client, reserve=reserved.append,
                                       checkpoint=lambda r: None, clock=lambda: NOW, monotonic=lambda: 0)
        self.assertEqual(len(requests), 9)
        self.assertEqual(reserved, [pf.RESERVATION_USD])
        self.assertTrue(record["usage_complete"])
        self.assertTrue(all(r["baseline"]["status"] == r["researched"]["status"] == "completed"
                            for r in record["records"]))


if __name__ == "__main__":
    unittest.main()
