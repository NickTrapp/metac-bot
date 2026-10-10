"""Portable synthetic diagnostic and gate tests, without SDK or credentials."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import research_compatibility_check as check
import research_compatibility_v1 as compatibility
import prospective_forecast as pf
import researched_forecast as rf
from test_prospective_diagnostics import BadRequestError, SECRET
from test_prospective_forecast import NOW
from test_researched_forecast import response, sdk_response

SHA = "a" * 40
PARENT = "b" * 40
ENV = {"GITHUB_REPOSITORY": "NickTrapp/metac-bot", "GITHUB_EVENT_NAME": "push",
       "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_RUN_ID": "123", "GITHUB_SHA": SHA, "RESEARCH_COMPATIBILITY_APPROVED": "true",
       "GITHUB_WORKFLOW_REF": f"NickTrapp/metac-bot/.github/workflows/{check.WORKFLOW}@refs/heads/main"}
HISTORY = {"total_count": 1, "workflow_runs": [{"id": 123, "head_sha": SHA,
    "head_branch": "main", "event": "push", "path": f".github/workflows/{check.WORKFLOW}",
    "run_number": 1, "run_attempt": 1, "status": "in_progress", "conclusion": None}]}
PRIOR_HISTORY = {"total_count": 1, "workflow_runs": [{"id": check.PRIOR_RUN_ID,
    "head_sha": check.PRIOR_TRIGGER_COMMIT, "head_branch": "main", "event": "push",
    "path": ".github/workflows/research_filter_comparison_20261010.yaml",
    "run_number": 1, "run_attempt": 1, "status": "completed", "conclusion": "success"}]}


def apollo_response():
    value = response("Synthetic Apollo evidence", research=True)
    for item in value["output"]:
        if item["type"] == "web_search_call":
            for source in item["action"]["sources"]:
                source["url"] = check.QUESTION["resolution_source_url"]
        if item["type"] == "message":
            for part in item["content"]:
                for annotation in part["annotations"]:
                    annotation["url"] = check.QUESTION["resolution_source_url"]
    return value


class GateTests(unittest.TestCase):
    def test_exact_context_and_history(self):
        check.verify_context(ENV)
        check.verify_history(ENV, HISTORY)

    def test_each_context_change_blocks(self):
        for key in ENV:
            with self.subTest(key=key), self.assertRaises(rf.ResearchBlocked):
                check.verify_context(ENV | {key: "unexpected"})

    def test_changed_missing_duplicate_history_blocks(self):
        for value in ({}, {"total_count": 2, "workflow_runs": HISTORY["workflow_runs"] * 2},
                      {"total_count": 1, "workflow_runs": []}):
            with self.assertRaises(rf.ResearchBlocked):
                check.verify_history(ENV, value)
        for key in HISTORY["workflow_runs"][0]:
            value = copy.deepcopy(HISTORY)
            value["workflow_runs"][0][key] = "wrong"
            with self.subTest(key=key), self.assertRaises(rf.ResearchBlocked):
                check.verify_history(ENV, value)

    def test_trigger_clean_separate_commit_required(self):
        valid = [SHA, "A\t" + check.TRIGGER_PATH, PARENT, ""]
        with patch.object(check.subprocess, "check_output", side_effect=valid), \
                patch.object(Path, "read_text", return_value=check.TRIGGER_TEXT):
            self.assertEqual(check.verify_trigger_commit(ENV), PARENT)
        for output in ([PARENT], [SHA, "M\t" + check.TRIGGER_PATH],
                       [SHA, "A\t" + check.TRIGGER_PATH + "\nM\tprospective_forecast.py"],
                       valid[:2] + ["invalid"], valid[:3] + ["M\tresearch_compatibility_check.py"]):
            with patch.object(check.subprocess, "check_output", side_effect=output), \
                    patch.object(Path, "read_text", return_value=check.TRIGGER_TEXT), \
                    self.assertRaises(rf.ResearchBlocked):
                check.verify_trigger_commit(ENV)
        with patch.object(check.subprocess, "check_output", side_effect=valid), \
                patch.object(Path, "read_text", return_value="wrong"), self.assertRaises(rf.ResearchBlocked):
            check.verify_trigger_commit(ENV)

    def test_invalid_context_prevents_history_read(self):
        with patch.object(check, "read_actions_json") as read:
            with self.assertRaises(rf.ResearchBlocked):
                check.check_gate({})
            read.assert_not_called()

    def test_gate_verifies_new_workflow_and_exact_prior_two_request_stage(self):
        with patch.object(check, "read_actions_json", side_effect=[HISTORY, PRIOR_HISTORY]) as read, \
                patch.object(check, "verify_trigger_commit", return_value=PARENT):
            self.assertEqual(check.check_gate(ENV), PARENT)
        self.assertEqual([call.args for call in read.call_args_list], [
            (ENV, f"workflows/{check.WORKFLOW}/runs?per_page=100"),
            (ENV, "workflows/research_filter_comparison_20261010.yaml/runs?per_page=100")])

    def test_prior_run_missing_changed_duplicated_or_retried_blocks(self):
        check.verify_prior_batch(PRIOR_HISTORY)
        for data in ({}, {"total_count": 2, "workflow_runs": PRIOR_HISTORY["workflow_runs"] * 2}):
            with self.assertRaises(rf.ResearchBlocked):
                check.verify_prior_batch(data)
        for key in PRIOR_HISTORY["workflow_runs"][0]:
            data = copy.deepcopy(PRIOR_HISTORY)
            data["workflow_runs"][0][key] = "wrong"
            with self.subTest(key=key), self.assertRaises(rf.ResearchBlocked):
                check.verify_prior_batch(data)

    def test_default_cli_offline(self):
        result = subprocess.run([sys.executable, "research_compatibility_check.py"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Offline only", result.stdout)

    def test_gate_failure_prevents_runner_and_artifact(self):
        with patch.object(sys, "argv", ["check", "--execute"]), \
                patch.object(check, "check_gate", side_effect=RuntimeError(SECRET)), \
                patch.object(check, "run_once") as run, patch.object(check, "save_checkpoint") as save:
            with self.assertRaisesRegex(SystemExit, "gate blocked"):
                check.main()
            run.assert_not_called()
            save.assert_not_called()

    def test_duplicate_claim_preserves_existing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            claim = Path(directory) / "claim"
            claim.write_text("prior claim")
            with patch.object(sys, "argv", ["check", "--execute"]), \
                    patch.object(check, "check_gate", return_value=PARENT), \
                    patch.dict(check.os.environ, ENV), patch.object(check, "CLAIM_PATH", claim), \
                    patch.object(check, "save_checkpoint") as save:
                with self.assertRaisesRegex(SystemExit, "claim failed"):
                    check.main()
                save.assert_not_called()
            self.assertEqual(claim.read_text(), "prior claim")

    def test_client_import_failure_preserves_claim_reservation_and_safe_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            claim = Path(directory) / "claim"
            snapshots = []
            with patch.object(sys, "argv", ["check", "--execute"]), \
                    patch.object(check, "check_gate", return_value=PARENT), \
                    patch.dict(check.os.environ, ENV), patch.dict(sys.modules, {"openai": None}), \
                    patch.object(check, "CLAIM_PATH", claim), patch("builtins.print"), \
                    patch.object(check, "save_checkpoint", side_effect=lambda r: snapshots.append(copy.deepcopy(r))):
                with self.assertRaisesRegex(SystemExit, "was not verified"):
                    check.main()
            self.assertTrue(claim.exists())
            self.assertEqual(snapshots[-1]["reserved_usd"], "0.50")
            self.assertEqual(snapshots[-1]["request_attempts"], 0)
            self.assertIsNone(snapshots[-1]["total_estimated_cost_usd"])
            self.assertEqual(snapshots[-1]["runner_failure_diagnostic"]["phase"], "runner")

    def test_workflow_is_separate_guarded_and_uses_only_existing_openai_secret(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows" / check.WORKFLOW).read_text()
        self.assertIn(f"paths: [{check.TRIGGER_PATH}]", workflow)
        self.assertIn("github.run_number == 1 && github.run_attempt == 1", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("METACULUS_TOKEN", workflow)
        self.assertNotIn("prospective_pilot.py", workflow)
        self.assertEqual(workflow.count("secrets."), 1)
        self.assertIn("secrets.OPENAI_API_KEY", workflow)
        self.assertIn("check_compatibility_sdk_offline.py", workflow)
        self.assertIn("poetry install --no-interaction --no-root --only main", workflow)
        self.assertLess(workflow.index("check_compatibility_sdk_offline.py"), workflow.index("--execute"))
        if (root / check.TRIGGER_PATH).exists():
            self.assertEqual((root / check.TRIGGER_PATH).read_text(), check.TRIGGER_TEXT)


def factual_response(answer="yes", url=None):
    url = url or check.QUESTION["resolution_source_url"]
    return response(json.dumps({"answer": answer, "rationale": "Synthetic resolved evidence",
                               "uncertainties": [], "source_urls": [url]}))


class CompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, results):
        client = Mock()
        create = client.with_options.return_value.responses.create = AsyncMock(
            side_effect=[v if isinstance(v, Exception) else sdk_response(v) for v in results])
        snapshots = []
        record = await check.run_pipeline(client, check.new_record(),
            checkpoint=lambda r: snapshots.append(copy.deepcopy(r)), clock=lambda: NOW)
        self.assertLessEqual(create.await_count, 2)
        self.assertEqual(record["request_attempts"], create.await_count)
        self.assertEqual(record["batch_request_attempts"], 2 + create.await_count)
        self.assertEqual(record["batch_request_limit"], 4)
        self.assertEqual(record["batch_reserved_usd"], "1.00")
        self.assertFalse(record["market_blind_research"])
        self.assertFalse(record["batch_usage_complete"])
        self.assertIsNone(record["total_batch_estimated_cost_usd"])
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertNotIn(SECRET, json.dumps(snapshots))
        self.assertNotIn("Synthetic resolved evidence", json.dumps(record))
        self.assertNotIn("Synthetic Apollo evidence", json.dumps(record))
        client.with_options.assert_called_once_with(base_url=rf.API_BASE, max_retries=0, timeout=90)
        return record, create

    async def test_corrected_research_then_tool_free_structured_fact_answer(self):
        record, create = await self.execute([apollo_response(), factual_response()])
        self.assertEqual(create.await_count, 2)
        first, second = [c.kwargs for c in create.await_args_list]
        expected = pf.research_request(check.QUESTION, pf.question_input(check.QUESTION, NOW.isoformat()))
        del expected["tools"][0]["filters"]
        self.assertEqual(first, expected)
        self.assertEqual(second["tools"], [])
        self.assertEqual(second["tool_choice"], "none")
        self.assertNotIn("max_tool_calls", second)
        self.assertEqual(second["text"]["format"]["schema"], compatibility.ANSWER_SCHEMA)
        self.assertEqual(record["operational_status"], "completed")
        self.assertEqual(record["factual_answer"]["answer"], "yes")
        self.assertTrue(record["known_fact_agrees"])
        self.assertEqual(record["source_provenance"]["citation_urls"], [check.QUESTION["resolution_source_url"]])
        self.assertTrue(record["usage_complete"])
        self.assertIsNotNone(record["total_estimated_cost_usd"])

    async def test_research_api_failure_prevents_answer_and_no_retry(self):
        error = BadRequestError()
        error.param = "tools"
        record, create = await self.execute([error])
        self.assertEqual(create.await_count, 1)
        self.assertEqual(record["requests"][0]["failure_diagnostic"]["provider_param"], "tools")
        self.assertEqual(record["operational_status"], "failed")
        self.assertFalse(record["usage_complete"])
        self.assertIsNone(record["total_estimated_cost_usd"])

    async def test_answer_api_failure_preserves_research_provenance_and_partial_cost(self):
        record, create = await self.execute([apollo_response(), BadRequestError()])
        self.assertEqual(create.await_count, 2)
        self.assertIsNotNone(record["source_provenance"])
        self.assertEqual(record["requests"][1]["failure_diagnostic"]["phase"], "sdk_request")
        self.assertEqual(record["known_estimated_cost_usd"], record["requests"][0]["estimated_cost_usd"])
        self.assertIsNone(record["total_estimated_cost_usd"])

    async def test_source_policy_failure_prevents_second_paid_request(self):
        invalid = apollo_response()
        invalid["output"][0]["action"]["sources"][0]["url"] = "https://outside.example/source"
        record, create = await self.execute([invalid])
        self.assertEqual(create.await_count, 1)
        self.assertEqual(record["requests"][0]["failure_diagnostic"]["phase"], "source_validate")
        self.assertNotIn("outside.example", json.dumps(record))

    async def test_unknown_answer_source_is_rejected_after_parse_without_retry(self):
        record, _ = await self.execute([apollo_response(), factual_response(url="https://nasa.gov/not-cited")])
        self.assertEqual(record["operational_status"], "failed")
        self.assertEqual(record["pipeline_failure_diagnostic"]["phase"], "answer_parse")
        self.assertIsNone(record["factual_answer"])
        self.assertTrue(record["usage_complete"])

    async def test_no_and_unknown_are_operational_mismatch_not_forecast_scores(self):
        for answer in ("no", "unknown"):
            record, _ = await self.execute([apollo_response(), factual_response(answer)])
            self.assertFalse(record["known_fact_agrees"])
            self.assertEqual(record["forecast_skill"], "not_evaluated")

    async def test_durable_failure_blocks_request(self):
        client = Mock()
        create = client.with_options.return_value.responses.create = AsyncMock()
        with self.assertRaises(OSError):
            await check.run_pipeline(client, check.new_record(), checkpoint=Mock(side_effect=OSError(SECRET)), clock=lambda: NOW)
        create.assert_not_called()

    async def test_reentry_or_expiry_prevents_configuration(self):
        for record, now in ((check.new_record(), rf.PRICING_REVIEW_DEADLINE),
                            (check.new_record() | {"request_attempts": 1}, NOW),
                            (check.new_record() | {"requests": [{}]}, NOW)):
            client = Mock()
            with self.assertRaises(rf.ResearchBlocked):
                await check.run_pipeline(client, record, checkpoint=Mock(), clock=lambda: now)
            client.with_options.assert_not_called()


class HelperTests(unittest.TestCase):
    def test_pinned_model_only(self):
        with patch.object(rf, "MODEL", "different-model"), self.assertRaises(rf.ResearchBlocked):
            compatibility.research_request(check.QUESTION, pf.question_input(check.QUESTION, NOW.isoformat()))

    def test_malformed_fact_answer_rejected(self):
        base = {"answer": "yes", "rationale": "Evidence", "uncertainties": [], "source_urls": ["https://nasa.gov/"]}
        for field, value in (("answer", []), ("answer", "maybe"), ("rationale", ""),
                             ("uncertainties", [False]), ("source_urls", []), ("source_urls", ["https://other.example/"])):
            with self.subTest(field=field), self.assertRaises(rf.ResearchBlocked):
                compatibility.parse_answer(json.dumps(base | {field: value}), {"https://nasa.gov/"})

    def test_public_provenance_omits_unsafe_urls(self):
        good = "https://www.nasa.gov/history/apollo-11/"
        bad = ["https://nasa.gov/?token=secret", "https://nasa.gov/#fragment", "http://nasa.gov/",
               "https://user:pass@nasa.gov/", "https://nasa.gov:443/", "https://nasa.gov.example.org/",
               "https://nasa.gov/\n", "https://nasa.gov/" + "x" * 2048]
        result = compatibility.source_provenance({"citations": [{"url":u} for u in [good,good]+bad], "sources": []})
        self.assertEqual(result["citation_urls"], [good])
        self.assertEqual(result["omitted_unsafe_urls"], len(bad))


if __name__ == "__main__":
    unittest.main()


