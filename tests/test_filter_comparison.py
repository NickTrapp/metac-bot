"""Portable synthetic diagnostic and gate tests, without SDK or credentials."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import research_filter_comparison as check
import prospective_forecast as pf
import researched_forecast as rf
from test_prospective_diagnostics import BadRequestError, SECRET
from test_prospective_forecast import NOW
from test_researched_forecast import response, sdk_response

SHA = "a" * 40
PARENT = "b" * 40
ENV = {"GITHUB_REPOSITORY": "NickTrapp/metac-bot", "GITHUB_EVENT_NAME": "push",
       "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_RUN_ID": "123", "GITHUB_SHA": SHA, "RESEARCH_FILTER_COMPARISON_APPROVED": "true",
       "GITHUB_WORKFLOW_REF": f"NickTrapp/metac-bot/.github/workflows/{check.WORKFLOW}@refs/heads/main"}
HISTORY = {"total_count": 1, "workflow_runs": [{"id": 123, "head_sha": SHA,
    "head_branch": "main", "event": "push", "path": f".github/workflows/{check.WORKFLOW}",
    "run_number": 1, "run_attempt": 1, "status": "in_progress", "conclusion": None}]}


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
                       valid[:2] + ["invalid"], valid[:3] + ["M\tresearch_filter_comparison.py"]):
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

    def test_gate_reads_only_new_workflow(self):
        with patch.object(check, "read_actions_json", return_value=HISTORY) as read, \
                patch.object(check, "verify_trigger_commit", return_value=PARENT):
            self.assertEqual(check.check_gate(ENV), PARENT)
        read.assert_called_once_with(ENV, f"workflows/{check.WORKFLOW}/runs?per_page=100")

    def test_default_cli_offline(self):
        result = subprocess.run([sys.executable, "research_filter_comparison.py"],
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
                with self.assertRaisesRegex(SystemExit, "stopped before"):
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
        self.assertIn("check_filter_comparison_sdk_offline.py", workflow)
        self.assertIn("poetry install --no-interaction --no-root --only main", workflow)
        self.assertLess(workflow.index("check_filter_comparison_sdk_offline.py"), workflow.index("--execute"))
        if (root / check.TRIGGER_PATH).exists():
            self.assertEqual((root / check.TRIGGER_PATH).read_text(), check.TRIGGER_TEXT)


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, results):
        client = Mock()
        create = client.with_options.return_value.responses.create = AsyncMock(
            side_effect=[r if isinstance(r, Exception) else sdk_response(r) for r in results])
        snapshots = []
        record = await check.run_comparison(client, check.new_record(),
            checkpoint=lambda r: snapshots.append(copy.deepcopy(r)), clock=lambda: NOW)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(record["request_attempts"], 2)
        self.assertEqual([r["variant"] for r in record["requests"]], ["filtered", "unfiltered"])
        self.assertEqual(record["reserved_usd"], "0.50")
        self.assertEqual(record["authorized_limit_usd"], "2.50")
        client.with_options.assert_called_once_with(base_url=rf.API_BASE, max_retries=0, timeout=90)
        self.assertEqual(record["operational_status"], "completed_two_attempts")
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertNotIn(SECRET, json.dumps(snapshots))
        self.assertNotIn("Synthetic Apollo evidence", json.dumps(record))
        self.assertEqual(snapshots[0]["request_attempts"], 0)
        self.assertEqual(snapshots[1]["request_attempts"], 1)
        self.assertEqual(snapshots[1]["requests"][0]["status"], "attempted")
        return record, create

    async def test_payloads_differ_only_in_domain_filter(self):
        record, create = await self.execute([apollo_response(), apollo_response()])
        filtered, unfiltered = [c.kwargs for c in create.await_args_list]
        control = copy.deepcopy(filtered)
        self.assertEqual(control["tools"][0].pop("filters"), {"allowed_domains": ["nasa.gov"]})
        self.assertEqual(control, unfiltered)
        self.assertEqual(filtered, pf.research_request(check.QUESTION,
            pf.question_input(check.QUESTION, NOW.isoformat())))
        self.assertEqual(record["comparison_outcome"], "both_requests_returned")
        self.assertTrue(record["usage_complete"])
        self.assertIsNotNone(record["total_estimated_cost_usd"])

    async def test_filtered_rejected_unfiltered_returned_is_isolated(self):
        error = BadRequestError()
        error.param = "tools[0].filters"
        record, _ = await self.execute([error, apollo_response()])
        first, second = record["requests"]
        self.assertEqual(first["failure_diagnostic"]["provider_param"], "tools[0].filters")
        self.assertEqual(first["failure_diagnostic"]["http_status"], 400)
        self.assertTrue(second["sdk_returned"])
        self.assertEqual(record["comparison_outcome"], "filtered_rejected_400_unfiltered_returned")
        self.assertFalse(record["usage_complete"])
        self.assertIsNone(record["total_estimated_cost_usd"])
        self.assertEqual(record["known_estimated_cost_usd"], second["estimated_cost_usd"])

    async def test_same_rejected_parameter_is_reported_without_extra_requests(self):
        errors = [BadRequestError(), BadRequestError()]
        for error in errors:
            error.param = "max_tool_calls"
        record, _ = await self.execute(errors)
        self.assertEqual(record["comparison_outcome"], "both_rejected_400_same_parameter")
        self.assertEqual(record["known_estimated_cost_usd"], "0")
        self.assertIsNone(record["total_estimated_cost_usd"])
        self.assertFalse(record["usage_complete"])

    async def test_unknown_or_transient_failures_stay_inconclusive_without_repeats(self):
        record, _ = await self.execute([TimeoutError(SECRET), RuntimeError(SECRET)])
        self.assertEqual(record["comparison_outcome"], "inconclusive")
        self.assertFalse(record["usage_complete"])
        self.assertIsNone(record["total_estimated_cost_usd"])

    async def test_local_source_failure_does_not_erase_api_acceptance(self):
        outside = apollo_response()
        outside["output"][0]["action"]["sources"][0]["url"] = "https://outside.example/source"
        record, _ = await self.execute([BadRequestError(), outside])
        second = record["requests"][1]
        self.assertTrue(second["sdk_returned"])
        self.assertEqual(second["failure_diagnostic"]["phase"], "source_validate")
        self.assertEqual(record["comparison_outcome"], "filtered_rejected_400_unfiltered_returned")
        self.assertNotIn("outside.example", json.dumps(record))

    async def test_missing_usage_never_becomes_zero_total(self):
        malformed = apollo_response()
        malformed["output"] = None
        record, _ = await self.execute([malformed, apollo_response()])
        self.assertTrue(record["requests"][0]["sdk_returned"])
        self.assertFalse(record["usage_complete"])
        self.assertIsNone(record["total_estimated_cost_usd"])

    async def test_pre_send_checkpoint_failure_prevents_request(self):
        for fail_on in (1, 2):
            count = 0
            def checkpoint(record):
                nonlocal count
                count += 1
                if count == fail_on:
                    raise OSError(SECRET)
            client = Mock()
            create = client.with_options.return_value.responses.create = AsyncMock()
            with self.assertRaises(OSError):
                await check.run_comparison(client, check.new_record(), checkpoint=checkpoint, clock=lambda: NOW)
            create.assert_not_called()

    async def test_checkpoint_failure_after_first_attempt_blocks_second(self):
        client = Mock()
        create = client.with_options.return_value.responses.create = AsyncMock(return_value=sdk_response(apollo_response()))
        count = 0
        def checkpoint(record):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError(SECRET)
        with self.assertRaises(OSError):
            await check.run_comparison(client, check.new_record(), checkpoint=checkpoint, clock=lambda: NOW)
        self.assertEqual(create.await_count, 1)

    async def test_expiry_or_reentry_prevents_client_configuration(self):
        for record, now in ((check.new_record(), rf.PRICING_REVIEW_DEADLINE),
                            (check.new_record() | {"request_attempts": 1}, NOW),
                            (check.new_record() | {"requests": [{}]}, NOW)):
            client = Mock()
            with self.assertRaises(rf.ResearchBlocked):
                await check.run_comparison(client, record, checkpoint=Mock(), clock=lambda: now)
            client.with_options.assert_not_called()

    async def test_known_cost_guard_prevents_next_request(self):
        client = Mock()
        create = client.with_options.return_value.responses.create = AsyncMock()
        def checkpoint(record):
            record["known_estimated_cost_usd"] = "2.30"
        record = await check.run_comparison(client, check.new_record(), checkpoint=checkpoint, clock=lambda: NOW)
        create.assert_not_called()
        self.assertEqual(record["operational_status"], "stopped_before_next_request")


if __name__ == "__main__":
    unittest.main()


