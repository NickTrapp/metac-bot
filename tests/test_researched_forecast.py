"""Synthetic fixtures only. No SDK import, credentials, search, or network."""
import copy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import researched_forecast as rf


NOW = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
SOURCE = "https://example.org/synthetic/coin-protocol"
QUESTION = rf.BinaryQuestion(
    title="Will the next synthetic fair coin flip be heads?",
    description="Offline fixture, not a real event.",
    resolution_criteria="Yes for heads, No for tails.",
    fine_print="Assume a fair independent flip with no edge landing.")
FORECAST = {"probability_yes": 0.5, "rationale": "The synthetic protocol specifies a fair coin.",
            "uncertainties": ["The fixture assumes the protocol is followed."],
            "source_urls": [SOURCE]}


def response(text, *, research=False):
    annotations = [{"type": "url_citation", "url": SOURCE, "title": "Synthetic protocol",
                    "start_index": 0, "end_index": len(text)}] if research else []
    output = [{"type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": text, "annotations": annotations}]}]
    if research:
        output.insert(0, {"type": "web_search_call", "status": "completed", "action": {
            "type": "search", "query": "synthetic coin protocol",
            "sources": [{"type": "url", "url": SOURCE}]}})
    return {"id": "resp_offline_fixture", "status": "completed", "model": rf.MODEL,
            "usage": {"input_tokens": 100, "output_tokens": 100}, "output": output}


def sdk_response(data):
    return SimpleNamespace(model_dump=lambda **kw: copy.deepcopy(data))


def setup(research=None, forecast=None):
    research = research if research is not None else response(
        "As of 2026-10-10 the synthetic protocol specifies a fair coin.", research=True)
    forecast = forecast if forecast is not None else response(json.dumps(FORECAST))
    create = AsyncMock(side_effect=[sdk_response(research), sdk_response(forecast)])
    client = Mock()
    client.with_options.return_value.responses.create = create
    return client, create, Mock()


class ResearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = patch.object(rf, "datetime", wraps=datetime)
        self.mock_clock = self.clock.start()
        self.mock_clock.now.return_value = NOW
        self.addCleanup(self.clock.stop)

    async def test_two_calls_research_then_tool_free_forecast(self):
        client, create, reserve = setup()
        result = await rf.forecast_binary(QUESTION, client, reserve)
        self.assertEqual(create.await_count, 2)
        reserve.assert_called_once_with(Decimal("0.25"))
        client.with_options.assert_called_once_with(base_url=rf.API_BASE, max_retries=0, timeout=90)
        research, forecast = [c.kwargs for c in create.await_args_list]
        self.assertEqual(research["tools"], [{"type": "web_search", "search_context_size": "low"}])
        self.assertEqual(research["max_tool_calls"], 2)
        self.assertEqual(research["tool_choice"], "required")
        self.assertEqual(research["include"], ["web_search_call.action.sources"])
        self.assertEqual(forecast["tools"], [])
        self.assertEqual(forecast["tool_choice"], "none")
        self.assertEqual(forecast["text"]["format"]["schema"], rf.FORECAST_SCHEMA)
        self.assertIn(SOURCE, forecast["input"])
        for request in (research, forecast):
            self.assertEqual(request["model"], rf.MODEL)
            self.assertEqual(request["max_output_tokens"], 3000)
            self.assertFalse(request["store"])
            self.assertEqual(request["service_tier"], "default")
            self.assertEqual(request["truncation"], "disabled")
            self.assertNotIn("previous_response_id", request)
            self.assertNotIn("conversation", request)
            self.assertLessEqual(len(rf._json(request).encode()), rf.MAX_REQUEST_BYTES)
        self.assertEqual(result["forecast"]["probability_yes"], 0.5)
        self.assertFalse(result["published"])
        self.assertEqual(result["forecast_skill"], "not_evaluated")
        self.assertEqual(result["created_at_utc"], NOW.isoformat())
        self.assertEqual(len(result["question_sha256"]), 64)
        citation = result["research"]["citations"][0]
        self.assertEqual(citation["accessed_at_utc"], NOW.isoformat())
        self.assertIsNone(citation["publication_date"])
        self.assertEqual(citation["cited_text"], result["research"]["text"])
        self.assertEqual(result["research"]["sources"][0]["url"], SOURCE)
        self.assertEqual(result["estimated_cost_usd"], "0.0136")

    async def test_reserve_before_first_request_and_keep_after_timeout(self):
        client, create, reserve = setup()
        async def timeout(**kw):
            reserve.assert_called_once()
            raise TimeoutError("secret provider message")
        create.side_effect = timeout
        with self.assertRaisesRegex(rf.ResearchBlocked, "reservation retained") as error:
            await rf.forecast_binary(QUESTION, client, reserve)
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(create.await_count, 1)
        reserve.assert_called_once()

    async def test_insufficient_budget_calls_no_provider(self):
        client, create, reserve = setup()
        reserve.side_effect = rf.ResearchBlocked("Insufficient budget")
        with self.assertRaisesRegex(rf.ResearchBlocked, "Insufficient"):
            await rf.forecast_binary(QUESTION, client, reserve)
        create.assert_not_called()

    async def test_invalid_question_and_expired_pricing_fail_before_reservation(self):
        for question in (rf.BinaryQuestion(""), rf.BinaryQuestion("x", description=None),
                         rf.BinaryQuestion("x", url="javascript:alert(1)"),
                         rf.BinaryQuestion("界" * 11000)):
            with self.subTest(question=question.title[:10]):
                client, create, reserve = setup()
                with self.assertRaises(rf.ResearchBlocked):
                    await rf.forecast_binary(question, client, reserve)
                create.assert_not_called()
                reserve.assert_not_called()
        self.mock_clock.now.return_value = rf.PRICING_REVIEW_DEADLINE
        client, create, reserve = setup()
        with self.assertRaisesRegex(rf.ResearchBlocked, "Recheck model"):
            await rf.forecast_binary(QUESTION, client, reserve)
        create.assert_not_called()
        reserve.assert_not_called()

    async def test_bad_research_never_reaches_forecast_or_retries(self):
        valid = response("Synthetic source evidence.", research=True)
        variants = []
        for field, value in (("model", "other-model"), ("status", "incomplete"),
                             ("usage", None), ("usage", {"input_tokens": True, "output_tokens": 1}),
                             ("usage", {"input_tokens": 100, "output_tokens": 3001})):
            data = copy.deepcopy(valid); data[field] = value; variants.append(data)
        data = copy.deepcopy(valid); data["output"].pop(0); variants.append(data)
        data = copy.deepcopy(valid); data["output"][1]["content"][0]["annotations"] = []; variants.append(data)
        data = copy.deepcopy(valid); data["output"] = [data["output"][0]] * 3 + data["output"][1:]; variants.append(data)
        data = copy.deepcopy(valid); data["output"][0]["action"]["type"] = "open_page"; variants.append(data)
        data = copy.deepcopy(valid); data["output"][0]["status"] = "failed"; variants.append(data)
        data = copy.deepcopy(valid); data["output"][1]["content"][0]["annotations"][0]["url"] = "file:///private"; variants.append(data)
        data = copy.deepcopy(valid); data["output"][1]["content"][0]["annotations"][0]["end_index"] = 999; variants.append(data)
        data = response("x" * rf.MAX_REQUEST_BYTES, research=True); variants.append(data)
        for data in variants:
            with self.subTest(data=data.get("status")):
                client, create, reserve = setup(research=data)
                with self.assertRaises(rf.ResearchBlocked):
                    await rf.forecast_binary(QUESTION, client, reserve)
                self.assertEqual(create.await_count, 1)
                reserve.assert_called_once()

    async def test_bad_forecast_fails_without_parse_repair(self):
        variants = ["not json", json.dumps(FORECAST | {"extra": "unexpected"})]
        for field, values in {"probability_yes": [-0.01, 1.01, True, "50%", float("nan")],
                              "rationale": ["", None], "uncertainties": [None, [1]],
                              "source_urls": [[], ["https://example.org/invented"], [None]]}.items():
            variants.extend(json.dumps(FORECAST | {field: value}) for value in values)
        for text in variants:
            client, create, reserve = setup(forecast=response(text))
            with self.assertRaises(rf.ResearchBlocked):
                await rf.forecast_binary(QUESTION, client, reserve)
            self.assertEqual(create.await_count, 2)
            reserve.assert_called_once()

    async def test_forecast_tool_call_is_rejected(self):
        client, create, reserve = setup(forecast=response(json.dumps(FORECAST), research=True))
        with self.assertRaisesRegex(rf.ResearchBlocked, "Unexpected or incomplete tool"):
            await rf.forecast_binary(QUESTION, client, reserve)
        self.assertEqual(create.await_count, 2)

    async def test_second_request_failure_does_not_restart_research(self):
        client, create, reserve = setup()
        create.side_effect = [sdk_response(response("Synthetic evidence", research=True)),
                              RuntimeError("raw SDK error")]
        with self.assertRaisesRegex(rf.ResearchBlocked, "request failed") as caught:
            await rf.forecast_binary(QUESTION, client, reserve)
        self.assertEqual(create.await_count, 2)
        reserve.assert_called_once()
        partial = caught.exception.partial_record
        self.assertEqual(partial["request_attempts"], 2)
        self.assertEqual(partial["research"]["citations"][0]["url"], SOURCE)
        self.assertEqual(partial["usage_observations"][0]["estimated_cost_usd"], "0.0134")

    async def test_parse_failure_retains_both_measured_usages(self):
        client, create, reserve = setup(forecast=response("not JSON"))
        with self.assertRaises(rf.ResearchBlocked) as caught:
            await rf.forecast_binary(QUESTION, client, reserve)
        self.assertEqual(len(caught.exception.partial_record["usage_observations"]), 2)
        self.assertEqual(caught.exception.partial_record["forecast_response"]["text"], "not JSON")

    async def test_second_search_and_multiple_text_parts_preserve_offsets(self):
        data = response("Synthetic evidence", research=True)
        data["output"].insert(0, copy.deepcopy(data["output"][0]))
        data["output"][-1]["content"].append(copy.deepcopy(data["output"][-1]["content"][0]))
        client, create, reserve = setup(research=data)
        result = await rf.forecast_binary(QUESTION, client, reserve)
        research = result["research"]
        self.assertEqual(len(research["searches"]), 2)
        for citation in research["citations"]:
            self.assertEqual(research["text"][citation["start_index"]:citation["end_index"]],
                             citation["cited_text"])


class EntrypointTests(unittest.TestCase):
    def test_script_is_offline_and_has_no_cli_execution_path(self):
        result = subprocess.run([sys.executable, "researched_forecast.py"], capture_output=True,
                                text=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        source = Path(rf.__file__).read_text()
        self.assertNotIn("os.environ", source)
        self.assertNotIn("AsyncOpenAI(", source)
        self.assertNotIn("http.client", source)
        self.assertNotIn("post_once", source)


if __name__ == "__main__":
    unittest.main()
