"""Only synthetic prospective events and mocked clients; no network/SDK/key."""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest
from unittest.mock import AsyncMock, Mock

import prospective_forecast as pf
import researched_forecast as rf
from test_researched_forecast import response, sdk_response, SOURCE

NOW = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)


def manifest(n=3):
    return {"schema_version": 1, "pilot_id": pf.PILOT_ID,
            "frozen_at_utc": (NOW - timedelta(minutes=1)).isoformat(),
            "questions": [{"question_id": f"synthetic-{i}", "title": f"Synthetic flip {i} heads?",
                "description": "Offline independent fair coin fixture.", "resolution_criteria": "Yes for heads; No for tails.",
                "fine_print": "No real event.", "url": f"https://example.org/fixture/{i}",
                "event_time_utc": (NOW + timedelta(days=1)).isoformat(),
                "resolution_source_url": SOURCE, "research_allowed_domains": ["example.org"], "public": True} for i in range(n)]}


def forecast(p=0.5, sources=None):
    return sdk_response(response(json.dumps({"probability_yes": p, "rationale": "A fair synthetic coin.",
        "uncertainties": [], "source_urls": [] if sources is None else sources})))


def evidence():
    return sdk_response(response("A synthetic protocol specifies a fair coin.", research=True))


def setup(n=3):
    create = AsyncMock(side_effect=[item for _ in range(n)
                                  for item in (forecast(), evidence(), forecast(sources=[SOURCE]))])
    client = Mock()
    client.with_options.return_value.responses.create = create
    return client, create


class ManifestTests(unittest.TestCase):
    def test_normalization_and_hash_are_stable(self):
        clean = pf.normalize_manifest(manifest(), now=NOW)
        self.assertEqual(pf.sha256(clean), pf.sha256(pf.normalize_manifest(clean, now=NOW)))
        self.assertEqual(len(clean["questions"]), 3)
        self.assertEqual(clean["questions"][0]["event_time_utc"], clean["questions"][0]["event_start_time_utc"])

    def test_limits_duplicate_ids_urls_and_missing_fields(self):
        bad = [manifest(2), manifest(6)]
        for key in ("question_id", "url"):
            data = manifest(); data["questions"][1][key] = data["questions"][0][key]; bad.append(data)
        for key in ("question_id", "title", "resolution_criteria", "resolution_source_url", "event_time_utc"):
            data = manifest(); del data["questions"][0][key]; bad.append(data)
        for value in (False, None, "true"):
            data = manifest(); data["questions"][0]["public"] = value; bad.append(data)
        for data in bad:
            with self.assertRaises((ValueError, rf.ResearchBlocked)):
                pf.normalize_manifest(data, now=NOW)

    def test_lead_strictly_greater_than_six_hours_and_future_freeze(self):
        for delta in (-1, 0, 6 * 3600):
            data = manifest(); data["questions"][0]["event_time_utc"] = (NOW + timedelta(seconds=delta)).isoformat()
            with self.assertRaisesRegex(ValueError, "six hours"):
                pf.normalize_manifest(data, now=NOW)
        data = manifest(); data["questions"][0]["event_time_utc"] = (NOW + timedelta(seconds=6 * 3600 + 1)).isoformat()
        pf.normalize_manifest(data, now=NOW)
        data["frozen_at_utc"] = (NOW + timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError, "frozen"):
            pf.normalize_manifest(data, now=NOW)

    def test_naive_timestamps_bad_urls_and_disagreeing_event_start_rejected(self):
        bad = []
        for key, value in (("event_time_utc", "2026-10-11T04:00:00"),
                           ("event_start_time_utc", "2026-10-12T04:00:00Z"),
                           ("expected_outcome_time_utc", "2026-10-09T04:00:00Z"),
                           ("url", "http://example.org/"), ("url", "https://example.org/?token=secret"),
                           ("url", "https://u:p@example.org/")):
            data = manifest(); data["questions"][0][key] = value; bad.append(data)
        for data in bad:
            with self.assertRaises(ValueError):
                pf.normalize_manifest(data, now=NOW)

    def test_market_probability_fields_are_stripped_before_hash_or_model_input(self):
        raw = manifest()
        raw["probability"] = 0.998123
        raw["questions"][0].update(probability=0.998123, probabilityHistory=[0.998123],
            outcome="YES", resolutionProbability=0.998123, liquidity=1000,
            answers=[{"probability": 0.998123}], p=0.998123, bets=[{"probBefore": 0.998123}])
        self.assertEqual(pf.normalize_manifest(raw, now=NOW), pf.normalize_manifest(manifest(), now=NOW))
        self.assertNotIn("0.998123", pf.canonical(pf.normalize_manifest(raw, now=NOW)))

    def test_primary_domain_allowlist_required_and_markets_blocked(self):
        for domains in ([], ["https://example.org"], ["example.org/"], ["example.org", "example.org"],
                        ["manifold.markets"], ["sub.metaculus.com"], "example.org"):
            raw = manifest(); raw["questions"][0]["research_allowed_domains"] = domains
            with self.subTest(domains=domains), self.assertRaises(ValueError):
                pf.normalize_manifest(raw, now=NOW)

    def test_oversized_question_fails_offline(self):
        data = manifest(); data["questions"][2]["description"] = "x" * 33000
        with self.assertRaises(rf.ResearchBlocked):
            pf.normalize_manifest(data, now=NOW)


class PilotTests(unittest.IsolatedAsyncioTestCase):
    async def run_fixture(self, client, raw=None, **kwargs):
        return await pf.run_pilot(raw or manifest(), client, reserve=kwargs.pop("reserve", Mock()),
            checkpoint=kwargs.pop("checkpoint", Mock()), clock=kwargs.pop("clock", lambda: NOW),
            monotonic=kwargs.pop("monotonic", lambda: 0), **kwargs)

    async def test_same_prompt_model_limits_stateless_baseline_first(self):
        client, create = setup()
        reserve, checkpoints = Mock(), []
        result = await self.run_fixture(client, reserve=reserve,
            checkpoint=lambda r: checkpoints.append(copy.deepcopy(r)))
        reserve.assert_called_once_with(Decimal("2.50"))
        client.with_options.assert_called_once_with(base_url=rf.API_BASE, max_retries=0, timeout=90)
        calls = [c.kwargs for c in create.await_args_list]
        self.assertEqual(len(calls), 9)
        for i in range(3):
            baseline, research, researched = calls[3*i:3*i+3]
            self.assertEqual(baseline["instructions"], researched["instructions"])
            self.assertEqual(baseline["tools"], [])
            self.assertEqual(researched["tools"], [])
            self.assertEqual(research["max_tool_calls"], 2)
            self.assertEqual(research["tools"][0]["filters"], {"allowed_domains": ["example.org"]})
            self.assertEqual(json.loads(baseline["input"])["research"], {})
            self.assertEqual(json.loads(baseline["input"])["question"], json.loads(researched["input"])["question"])
            self.assertNotIn("/fixture/", baseline["input"])
            self.assertNotIn("synthetic-", baseline["input"])
        for call in calls:
            self.assertEqual(call["model"], rf.MODEL)
            self.assertEqual(call["max_output_tokens"], rf.MAX_OUTPUT_TOKENS)
            self.assertFalse(call["store"])
            self.assertNotIn("previous_response_id", call)
            self.assertNotIn("conversation", call)
        self.assertEqual(result["request_attempts"], 9)
        self.assertEqual(result["search_allowance_consumed"], 6)
        self.assertEqual(result["observed_search_calls"], 3)
        self.assertTrue(result["usage_complete"])
        self.assertEqual(result["manifest_sha256"], pf.sha256(result["manifest"]))
        self.assertEqual(result["protocol_sha256"], pf.sha256(result["protocol"]))
        self.assertEqual(result["forecast_skill"], "not_evaluated")
        self.assertEqual(len(checkpoints[0]["records"]), 3)
        self.assertTrue(all(r["baseline"]["operational_probability_yes"] == 0.5 for r in checkpoints[0]["records"]))
        for record in result["records"]:
            self.assertTrue(record["pair_valid"])
            self.assertEqual(record["baseline"]["forecast"]["source_urls"], [])
            for arm in ("baseline", "researched"):
                self.assertEqual(record[arm]["status"], "completed")
                for request in record[arm]["requests"]:
                    self.assertEqual(request["started_at_utc"], NOW.isoformat())
                    self.assertEqual(request["completed_at_utc"], NOW.isoformat())
                    self.assertEqual(len(request["request_sha256"]), 64)
                    self.assertIsNotNone(request["usage_observation"])

    async def test_five_questions_never_exceed_fifteen_requests_ten_search_allowance(self):
        client, create = setup(5)
        result = await self.run_fixture(client, manifest(5))
        self.assertEqual(create.await_count, 15)
        self.assertEqual(result["search_allowance_consumed"], 10)

    async def test_failed_baseline_still_runs_research_no_retry_and_retains_half(self):
        client, create = setup()
        create.side_effect = [TimeoutError("secret provider error"), evidence(), forecast(.7, [SOURCE])] + [
            item for _ in range(2) for item in (forecast(), evidence(), forecast(sources=[SOURCE]))]
        result = await self.run_fixture(client)
        self.assertEqual(create.await_count, 9)
        first = result["records"][0]
        self.assertEqual(first["baseline"]["status"], "failed")
        self.assertIsNone(first["baseline"]["probability_yes"])
        self.assertEqual(first["baseline"]["operational_probability_yes"], .5)
        self.assertEqual(first["researched"]["probability_yes"], .7)
        self.assertFalse(result["usage_complete"])
        self.assertNotIn("secret provider", pf.canonical(result))

    async def test_failed_research_skips_forecast_retains_question_and_known_usage(self):
        client, create = setup()
        invalid = response("No citations", research=False)
        create.side_effect = [forecast(), sdk_response(invalid)] + [item for _ in range(2)
            for item in (forecast(), evidence(), forecast(sources=[SOURCE]))]
        result = await self.run_fixture(client)
        self.assertEqual(create.await_count, 8)
        self.assertEqual(len(result["records"]), 3)
        self.assertEqual(result["records"][0]["researched"]["operational_probability_yes"], .5)
        self.assertTrue(result["usage_complete"])
        self.assertIsNotNone(result["records"][0]["researched"]["requests"][0]["usage_observation"])

    async def test_empty_sources_allowed_only_baseline(self):
        client, create = setup()
        create.side_effect = [forecast(), evidence(), forecast()] + [item for _ in range(2)
            for item in (forecast(), evidence(), forecast(sources=[SOURCE]))]
        result = await self.run_fixture(client)
        self.assertEqual(result["records"][0]["baseline"]["status"], "completed")
        self.assertEqual(result["records"][0]["researched"]["status"], "failed")
        with self.assertRaises(rf.ResearchBlocked):
            rf._parse_forecast(json.dumps({"probability_yes": .5, "rationale": "x", "uncertainties": [], "source_urls": []}), set())

    async def test_timing_overrun_blocks_further_pair_requests_no_replacement(self):
        client, create = setup()
        ticks = [0]
        async def slow(**kwargs):
            ticks[0] += 601
            return forecast()
        create.side_effect = slow
        result = await self.run_fixture(client, clock=lambda: NOW + timedelta(seconds=ticks[0]), monotonic=lambda: ticks[0])
        self.assertEqual(create.await_count, 3)  # One baseline on each retained question.
        self.assertTrue(all(not r["pair_valid"] for r in result["records"]))
        self.assertTrue(all(r["researched"]["status"] == "failed" for r in result["records"]))
        self.assertTrue(all(r["timing_status"] == "deadline_exceeded" for r in result["records"]))

    async def test_invalid_manifest_and_budget_failure_send_no_requests(self):
        client, create = setup()
        with self.assertRaises(ValueError):
            await self.run_fixture(client, manifest(2))
        create.assert_not_called()
        with self.assertRaises(RuntimeError):
            await self.run_fixture(client, reserve=Mock(side_effect=RuntimeError("No allowance")))
        create.assert_not_called()

    async def test_checkpoint_failure_stops_instead_of_sending_unlogged_requests(self):
        client, create = setup()
        count = [0]
        def checkpoint(record):
            count[0] += 1
            if count[0] == 2:
                raise OSError("Cannot retain attempts")
        with self.assertRaises(OSError):
            await self.run_fixture(client, checkpoint=checkpoint)
        create.assert_not_called()

    async def test_reservation_and_attempt_checkpoint_precede_network(self):
        client, create = setup()
        reserved, snapshots = [], []
        queue = [item for _ in range(3) for item in (forecast(), evidence(), forecast(sources=[SOURCE]))]
        async def check(**kwargs):
            self.assertEqual(reserved, [pf.RESERVATION_USD])
            self.assertEqual(snapshots[-1]["request_attempts"], create.await_count)
            return queue.pop(0)
        create.side_effect = check
        await self.run_fixture(client, reserve=reserved.append, checkpoint=lambda r: snapshots.append(copy.deepcopy(r)))

    async def test_unobserved_attempt_checkpoint_marks_usage_incomplete(self):
        client, create = setup(); snapshots = []
        await self.run_fixture(client, checkpoint=lambda r: snapshots.append(copy.deepcopy(r)))
        for snapshot in snapshots:
            pending = [request for record in snapshot["records"] for arm in ("baseline", "researched")
                       for request in record[arm]["requests"] if request["status"] == "attempted"]
            if pending:
                self.assertFalse(snapshot["usage_complete"])

    async def test_slow_checkpoint_cannot_send_after_pair_deadline(self):
        client, create = setup(2); seconds = [0]
        def checkpoint(record):
            if record["request_attempts"] == 1 and seconds[0] == 0:
                seconds[0] = 601
        result = await self.run_fixture(client, checkpoint=checkpoint,
            clock=lambda: NOW + timedelta(seconds=seconds[0]), monotonic=lambda: seconds[0])
        self.assertEqual(create.await_count, 6)
        first = result["records"][0]
        self.assertEqual(first["baseline"]["requests"][0]["status"], "not_sent_deadline")
        self.assertFalse(first["pair_valid"])
        self.assertTrue(result["usage_complete"])

    async def test_completion_must_still_have_six_hour_margin(self):
        raw = manifest()
        for q in raw["questions"]:
            q["event_time_utc"] = (NOW + timedelta(hours=6, seconds=1)).isoformat()
        client, create = setup(); seconds = [0]
        async def late(**kwargs):
            seconds[0] = 2
            return forecast()
        create.side_effect = late
        result = await self.run_fixture(client, raw,
            clock=lambda: NOW + timedelta(seconds=seconds[0]), monotonic=lambda: seconds[0])
        self.assertEqual(create.await_count, 1)
        self.assertFalse(result["records"][0]["pair_valid"])
        self.assertEqual(result["records"][1]["timing_status"], "insufficient_lead")
        self.assertEqual(len(result["records"]), 3)

    async def test_search_citation_outside_frozen_primary_domains_fails(self):
        client, create = setup()
        bad = response("Wrong-domain evidence.", research=True)
        bad["output"][1]["content"][0]["annotations"][0]["url"] = "https://example.org.evil.test/fake"
        create.side_effect = [forecast(), sdk_response(bad)] + [item for _ in range(2)
            for item in (forecast(), evidence(), forecast(sources=[SOURCE]))]
        result = await self.run_fixture(client)
        self.assertEqual(create.await_count, 8)
        self.assertEqual(result["records"][0]["researched"]["status"], "failed")
        self.assertIsNotNone(result["records"][0]["researched"]["requests"][0]["response"])

    async def test_pricing_expiry_fails_before_reservation_or_calls(self):
        client, create = setup(); reserve = Mock()
        with self.assertRaises(rf.ResearchBlocked):
            await self.run_fixture(client, reserve=reserve, clock=lambda: rf.PRICING_REVIEW_DEADLINE)
        create.assert_not_called(); reserve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
