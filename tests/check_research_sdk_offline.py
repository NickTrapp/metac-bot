"""Real locked SDK serialization with httpx.MockTransport; no network or keys."""
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
    raise AssertionError("Network is forbidden in the SDK offline check")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

import httpx
import openai
from researched_forecast import BinaryQuestion, MODEL, forecast_binary


class SDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_sdk_both_payloads_and_optional_source_list(self):
        root = Path(__file__).resolve().parents[1]
        lock = tomllib.loads((root / "poetry.lock").read_text())
        expected = next(p["version"] for p in lock["package"] if p["name"] == "openai")
        self.assertEqual(openai.__version__, expected, "Use the existing Poetry lockfile")
        requests = []
        url = "https://example.org/offline-fixture"

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(str(request.url), "https://api.openai.com/v1/responses")
            self.assertEqual(request.method, "POST")
            if len(requests) == 1:
                self.assertEqual(payload["max_tool_calls"], 2)
                self.assertEqual(payload["tool_choice"], "required")
                text = "Synthetic evidence"
                annotation = [{"type": "url_citation", "url": url, "title": "Fixture",
                               "start_index": 0, "end_index": len(text)}]
                # sources is intentionally absent: SDK serializes it as None.
                search = [{"id": "ws_fake", "type": "web_search_call", "status": "completed",
                           "action": {"type": "search", "query": "synthetic"}}]
            else:
                self.assertEqual(payload["tools"], [])
                self.assertEqual(payload["tool_choice"], "none")
                text = json.dumps({"probability_yes": 0.5, "rationale": "Synthetic fair coin",
                                   "uncertainties": [], "source_urls": [url]})
                annotation, search = [], []
            return httpx.Response(200, json={"id": "resp_fake", "object": "response", "created_at": 1,
                "status": "completed", "model": MODEL, "error": None, "incomplete_details": None,
                "output": search + [{"id": "msg_fake", "type": "message", "role": "assistant",
                                     "status": "completed", "content": [{"type": "output_text",
                                     "text": text, "annotations": annotation}]}],
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}})

        reserved = []
        async with openai.AsyncOpenAI(api_key="offline-placeholder", max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            record = await forecast_binary(BinaryQuestion("Synthetic fair coin?"), client, reserved.append)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(reserved), 1)
        self.assertEqual(record["forecast"]["probability_yes"], 0.5)
        self.assertEqual(record["research"]["sources"], [])
        self.assertEqual(record["research"]["citations"][0]["url"], url)


if __name__ == "__main__":
    unittest.main()
