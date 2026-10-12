"""Operational and evaluation checks; no API key or network access required."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_loop as loop

NOW = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
URL = "https://www.nasa.gov/mission/apollo-11/?utm_source=openai"


def manifest(*, prospective=False, count=1):
    raw = json.loads((Path(__file__).resolve().parents[1] / "research-loop-development.json").read_text())
    if prospective:
        raw["mode"] = "prospective"
    raw["questions"] = [copy.deepcopy(raw["questions"][0]) for _ in range(count)]
    for i, question in enumerate(raw["questions"]):
        question["question_id"] += str(i)
        question["url"] += "fixture" + str(i)
        if prospective:
            question["event_time_utc"] = (NOW + timedelta(days=2)).isoformat()
    return loop.normalize_manifest(raw, NOW)


def response(stage, *, source=URL, probability=.8, model=None):
    if stage == "research":
        text = "NASA documents the Apollo 11 landing."
        search = [{"type": "web_search_call", "status": "completed",
                   "action": {"type": "search", "sources": [{"url": source}]}}]
        annotations = [{"type": "url_citation", "url": source,
                        "start_index": 0, "end_index": len(text)}]
    else:
        text = json.dumps({"probability_yes": probability, "rationale": "Synthetic fixture",
                           "uncertainties": [], "source_urls": [] if stage == "baseline" else [source]})
        search, annotations = [], []
    return {"id": "resp_fixture", "model": model or loop.PROTOCOL["research_model" if stage == "research" else "forecast_model"],
            "status": "completed", "error": None, "incomplete_details": None,
            "output": [{"type": "reasoning", "summary": []}] + search + [
                {"type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": text, "annotations": annotations}]}],
            "usage": {"input_tokens": 100, "output_tokens": 40}}


def sdk(data):
    return Mock(model_dump=Mock(return_value=data))


class LoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "loop.sqlite"
        self.ledger = loop.Ledger(self.path, manifest(), Decimal("1"))
        self.addCleanup(self.ledger.close)
        self.client = Mock()
        self.client.responses.create = AsyncMock(side_effect=[sdk(response(s)) for s in loop.STAGES])
        self.checkpoints = []

    async def run_fixture(self, **kwargs):
        return await loop.run_loop(self.ledger, self.client, checkpoint=self.checkpoints.append,
                                   clock=kwargs.get("clock", lambda: NOW))

    async def test_complete_pipeline_and_resume_never_repeat_paid_calls(self):
        result = await self.run_fixture()
        self.assertEqual(result["completed_pairs"], 1)
        self.assertEqual(result["eligible_pairs"], 0)
        self.assertEqual(result["reserved_usd"], "0.75")
        self.assertTrue(result["usage_complete"])
        self.assertEqual(self.client.responses.create.await_count, 3)
        self.assertEqual(self.checkpoints[1]["records"][0]["stages"]["baseline"]["status"], "in_flight")
        self.assertIn("utm_source", result["records"][0]["stages"]["research"]["value"]["citations"][0]["url"])
        again = await self.run_fixture()
        self.assertEqual(again["request_attempts"], 3)
        self.assertEqual(self.client.responses.create.await_count, 3)
        self.ledger.close()
        self.ledger = loop.Ledger(self.path, manifest(), Decimal("1.00"))
        self.addCleanup(self.ledger.close)
        await self.run_fixture()
        self.assertEqual(self.client.responses.create.await_count, 3)

    async def test_provider_error_stops_and_unknown_cost_remains_unknown(self):
        error = RuntimeError("private provider details must not be exported")
        error.status_code = 400
        self.client.responses.create.side_effect = [error]
        with self.assertRaisesRegex(loop.LoopBlocked, "provider_http_400"):
            await self.run_fixture()
        report = self.ledger.report()
        self.assertEqual(report["request_attempts"], 1)
        self.assertFalse(report["usage_complete"])
        self.assertEqual(report["reserved_usd"], "0.25")
        self.assertNotIn("private provider", json.dumps(report))
        with self.assertRaisesRegex(loop.LoopBlocked, "prior_failed_or_ambiguous"):
            await self.run_fixture()
        self.assertEqual(self.client.responses.create.await_count, 1)

    async def test_crash_after_reservation_is_not_retried(self):
        self.client.responses.create.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_fixture()
        self.assertEqual(self.ledger.attempt(manifest()["questions"][0]["question_id"], "baseline")["status"], "in_flight")
        with self.assertRaisesRegex(loop.LoopBlocked, "prior_failed_or_ambiguous"):
            await self.run_fixture()
        self.assertEqual(self.client.responses.create.await_count, 1)

    async def test_made_up_forecast_source_fails_and_retains_usage(self):
        self.client.responses.create.side_effect = [sdk(response("baseline")), sdk(response("research")),
                                                  sdk(response("forecast", source="https://nasa.gov/invented"))]
        with self.assertRaisesRegex(loop.LoopBlocked, "forecast_schema_or_provenance"):
            await self.run_fixture()
        attempt = self.ledger.report()["records"][0]["stages"]["forecast"]
        self.assertEqual(attempt["status"], "failed")
        self.assertIsNotNone(attempt["metadata"]["usage"])
        self.assertEqual(self.ledger.report()["completed_pairs"], 0)

    async def test_resume_partial_success_uses_saved_research(self):
        q = manifest()["questions"][0]
        for stage in ("baseline", "research"):
            payload = loop.build_request(stage, q, NOW.isoformat())
            self.ledger.reserve(q["question_id"], stage, payload, NOW.isoformat())
            value = loop.parse_response(response(stage), payload, q, stage, None)
            self.ledger.finish(q["question_id"], stage, value=value,
                               metadata=loop.response_metadata(response(stage), payload["model"]),
                               code=None, completed_at=NOW.isoformat())
        self.client.responses.create.side_effect = [sdk(response("forecast"))]
        result = await self.run_fixture()
        self.assertEqual(result["completed_pairs"], 1)
        self.assertEqual(self.client.responses.create.await_count, 1)
        payload = self.client.responses.create.call_args.kwargs
        self.assertEqual(payload["text"]["format"]["schema"]["properties"]["source_urls"]["items"]["enum"], [URL])

    async def test_insufficient_full_campaign_budget_blocks_before_any_call(self):
        self.ledger.close()
        self.path.unlink()
        self.ledger = loop.Ledger(self.path, manifest(), Decimal("0.50"))
        self.addCleanup(self.ledger.close)
        with self.assertRaisesRegex(loop.LoopBlocked, "full_campaign_exceeds"):
            await self.run_fixture()
        self.client.responses.create.assert_not_called()

    async def test_observed_cost_above_reservation_blocks_following_request(self):
        expensive = response("baseline")
        expensive["usage"]["input_tokens"] = 1_000_000
        self.client.responses.create.side_effect = [sdk(expensive)]
        with self.assertRaisesRegex(loop.LoopBlocked, "reservation_budget_exhausted"):
            await self.run_fixture()
        self.assertEqual(self.client.responses.create.await_count, 1)
        self.assertGreater(Decimal(self.ledger.report()["consumed_allowance_usd"]), Decimal("1"))

    async def test_expired_prospective_manifest_cannot_make_calls(self):
        self.ledger.close()
        self.path.unlink()
        self.ledger = loop.Ledger(self.path, manifest(prospective=True), Decimal("1"))
        self.addCleanup(self.ledger.close)
        with self.assertRaisesRegex(loop.LoopBlocked, "prospective_lead_expired"):
            await self.run_fixture(clock=lambda: NOW + timedelta(days=2))
        self.client.responses.create.assert_not_called()

    async def test_completion_after_pair_deadline_is_ineligible(self):
        current = [NOW]
        stages = iter(loop.STAGES)
        async def slow_forecast(**payload):
            stage = next(stages)
            if stage == "forecast":
                current[0] = NOW + timedelta(seconds=601)
            return sdk(response(stage))
        self.client.responses.create.side_effect = slow_forecast
        with self.assertRaisesRegex(loop.LoopBlocked, "completed_pair_missed_deadline"):
            await self.run_fixture(clock=lambda: current[0])
        result = self.ledger.report()
        self.assertEqual(result["completed_pairs"], 1)
        self.assertFalse(result["records"][0]["timing_valid"])

    async def test_scoring_prospective_pairs_keeps_failure_denominator(self):
        self.ledger.close()
        self.path.unlink()
        self.ledger = loop.Ledger(self.path, manifest(prospective=True, count=2), Decimal("2"))
        self.addCleanup(self.ledger.close)
        self.client.responses.create.side_effect = [sdk(response(s)) for s in loop.STAGES] + [RuntimeError("failed second question")]
        with self.assertRaises(loop.LoopBlocked):
            await self.run_fixture()
        result = self.ledger.report()
        labels = {"schema_version": 1, "outcomes": [
            {"question_id": q["question_id"], "outcome": 1,
             "resolved_at_utc": (NOW + timedelta(days=3)).isoformat(), "source_url": URL}
            for q in manifest(prospective=True, count=2)["questions"]]}
        scored = loop.score(result, labels)
        self.assertEqual(scored["frozen_questions"], 2)
        self.assertEqual(scored["resolved_questions"], 2)
        self.assertEqual(scored["scored_pairs"], 1)
        self.assertEqual(scored["mean_brier_delta"], 0)
        labels["outcomes"][0]["resolved_at_utc"] = (NOW - timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(loop.LoopBlocked, "outcome_predates"):
            loop.score(result, labels)


class ValidationTests(unittest.TestCase):
    def test_baseline_and_assisted_forecast_have_same_model_reasoning_and_prompt(self):
        q = manifest()["questions"][0]
        research = {"text": "Fixture", "citations": [{"url": URL}]}
        baseline = loop.build_request("baseline", q, NOW.isoformat())
        forecast = loop.build_request("forecast", q, NOW.isoformat(), research)
        for key in ("model", "reasoning", "instructions", "max_output_tokens", "store", "tools"):
            self.assertEqual(baseline[key], forecast[key])
        self.assertNotIn("temperature", forecast)
        self.assertNotIn("question_id", json.loads(forecast["input"])["question"])
        self.assertEqual(loop.build_request("research", q, NOW.isoformat())["tools"][0]["filters"]["allowed_domains"], ["nasa.gov"])

    def test_primary_url_preserves_tracking_query_and_rejects_host_spoofing(self):
        self.assertTrue(loop.primary_url(URL, ["nasa.gov"]))
        for bad in ("https://nasa.gov.attacker.com/x", "https://user:pass@nasa.gov/x", "https://nasa.gov:443/x", "https://nasa.gov/x\n"):
            self.assertFalse(loop.primary_url(bad, ["nasa.gov"]))

    def test_parse_rejects_sources_outside_allowed_domains_and_invalid_spans(self):
        q = manifest()["questions"][0]
        payload = loop.build_request("research", q, NOW.isoformat())
        bad = response("research")
        bad["output"][1]["action"]["sources"].append({"url": "https://metaculus.com/"})
        with self.assertRaisesRegex(loop.LoopBlocked, "retrieved_source_outside"):
            loop.parse_response(bad, payload, q, "research", None)
        bad = response("research")
        bad["output"][2]["content"][0]["annotations"][0]["end_index"] = 10000
        with self.assertRaisesRegex(loop.LoopBlocked, "invalid_citation"):
            loop.parse_response(bad, payload, q, "research", None)

    def test_identity_and_usage_validation(self):
        q = manifest()["questions"][0]
        payload = loop.build_request("baseline", q, NOW.isoformat())
        loop.parse_response(response("baseline", model="gpt-6.1-sol-2026-09-24"), payload, q, "baseline", None)
        for field, value in (("model", "gpt-6.1-sol-other"), ("status", "incomplete"), ("id", "")):
            bad = response("baseline")
            bad[field] = value
            with self.assertRaises(loop.LoopBlocked):
                loop.parse_response(bad, payload, q, "baseline", None)
        bad = response("baseline")
        bad["usage"]["input_tokens"] = True
        with self.assertRaisesRegex(loop.LoopBlocked, "invalid_usage"):
            loop.parse_response(bad, payload, q, "baseline", None)

    def test_atomic_reservations_and_immutable_definition(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "loop.sqlite"
            a = loop.Ledger(path, manifest(), Decimal("0.25"))
            b = loop.Ledger(path, manifest(), Decimal("0.25"))
            self.addCleanup(a.close)
            self.addCleanup(b.close)
            q = manifest()["questions"][0]
            payload = loop.build_request("baseline", q, NOW.isoformat())
            a.reserve(q["question_id"], "baseline", payload, NOW.isoformat())
            with self.assertRaisesRegex(loop.LoopBlocked, "attempt_already"):
                b.reserve(q["question_id"], "baseline", payload, NOW.isoformat())
            with self.assertRaisesRegex(loop.LoopBlocked, "reservation_budget_exhausted"):
                b.reserve(q["question_id"], "research", payload, NOW.isoformat())
            with self.assertRaisesRegex(loop.LoopBlocked, "ledger_definition_changed"):
                loop.Ledger(path, manifest(), Decimal("1"))

    def test_development_fixture_cannot_be_scored_as_forecast_skill(self):
        with self.assertRaisesRegex(loop.LoopBlocked, "development_is_not"):
            loop.score({"mode": "development"}, {})


if __name__ == "__main__":
    unittest.main()
