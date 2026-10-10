"""Portable synthetic diagnostic and gate tests, without SDK or credentials."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import research_diagnostic_check as check
import prospective_forecast as pf
import researched_forecast as rf
from test_prospective_diagnostics import BadRequestError, SECRET
from test_prospective_forecast import NOW
from test_researched_forecast import response, sdk_response

SHA = "a" * 40
PARENT = "b" * 40
ENV = {"GITHUB_REPOSITORY": "NickTrapp/metac-bot", "GITHUB_EVENT_NAME": "push",
       "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_RUN_ID": "123", "GITHUB_SHA": SHA, "RESEARCH_DIAGNOSTIC_APPROVED": "true",
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


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, result):
        client = Mock()
        create = client.with_options.return_value.responses.create = AsyncMock()
        if isinstance(result, Exception):
            create.side_effect = result
        else:
            create.return_value = sdk_response(result)
        snapshots = []
        record = await check.run_probe(client, check.new_record(),
            checkpoint=lambda r: snapshots.append(copy.deepcopy(r)), clock=lambda: NOW)
        self.assertEqual(create.await_count, 1)
        client.with_options.assert_called_once_with(base_url=rf.API_BASE, max_retries=0, timeout=90)
        self.assertEqual(record["request_attempts"], 1)
        self.assertEqual(record["reserved_usd"], "0.25")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0]["operational_status"], "attempted")
        self.assertNotIn(SECRET, json.dumps(record))
        self.assertNotIn(SECRET, json.dumps(snapshots))
        self.assertNotIn("Synthetic Apollo evidence", json.dumps(record))
        self.assertNotIn("response", record)
        self.assertNotIn("text", record)
        return record, create

    async def test_success_uses_exact_prospective_builder_and_only_one_request(self):
        record, create = await self.execute(apollo_response())
        payload = pf.research_request(check.QUESTION, pf.question_input(check.QUESTION, NOW.isoformat()))
        create.assert_awaited_once_with(**payload)
        self.assertEqual(payload["tools"][0]["filters"], {"allowed_domains": ["nasa.gov"]})
        self.assertEqual(payload["max_tool_calls"], 2)
        self.assertEqual(record["request_sha256"], pf.sha256(payload))
        self.assertEqual(record["operational_status"], "completed")
        self.assertTrue(record["sources_validated"])
        self.assertTrue(record["usage_complete"])
        self.assertIsNone(record["failure_diagnostic"])
        self.assertGreater(record["citation_count"], 0)
        self.assertEqual(record["forecast_skill"], "not_evaluated")

    async def test_api_failure_keeps_only_safe_fields_with_unknown_cost(self):
        record, _ = await self.execute(BadRequestError())
        self.assertEqual(record["failure_diagnostic"], {
            "phase": "sdk_request", "response_received": False,
            "exception_class": "BadRequestError", "http_status": 400,
            "provider_code": "unsupported_parameter"})
        self.assertIsNone(record["estimated_cost_usd"])
        self.assertFalse(record["usage_complete"])

    async def test_unknown_or_local_exception_never_logs_raw_message(self):
        for error in (RuntimeError(SECRET), NameError(SECRET), TimeoutError(SECRET)):
            record, _ = await self.execute(error)
            self.assertEqual(record["failure_diagnostic"]["phase"], "sdk_request")
            self.assertFalse(record["failure_diagnostic"]["response_received"])

    async def test_malformed_response_retains_metadata_without_false_cost(self):
        value = apollo_response()
        value["output"] = None
        record, _ = await self.execute(value)
        self.assertEqual(record["failure_diagnostic"]["phase"], "response_validate")
        self.assertTrue(record["failure_diagnostic"]["response_received"])
        self.assertEqual(record["response_id"], "resp_offline_fixture")
        self.assertIsNone(record["usage_observation"])
        self.assertIsNone(record["estimated_cost_usd"])
        self.assertFalse(record["usage_complete"])

    async def test_source_failure_keeps_usage_but_no_source_or_text(self):
        value = apollo_response()
        value["output"][0]["action"]["sources"][0]["url"] = "https://outside.example/secret-query"
        record, _ = await self.execute(value)
        self.assertEqual(record["failure_diagnostic"]["phase"], "source_validate")
        self.assertTrue(record["usage_complete"])
        self.assertIsNotNone(record["estimated_cost_usd"])
        self.assertFalse(record["sources_validated"])
        self.assertNotIn("outside.example", json.dumps(record))

    async def test_metadata_failure_retains_response_received(self):
        value = apollo_response()
        value["usage"] = "bad"
        record, _ = await self.execute(value)
        self.assertEqual(record["failure_diagnostic"]["phase"], "response_metadata")
        self.assertTrue(record["failure_diagnostic"]["response_received"])

    async def test_durable_failure_prevents_send(self):
        client = Mock()
        create = client.with_options.return_value.responses.create = AsyncMock()
        with self.assertRaises(OSError):
            await check.run_probe(client, check.new_record(),
                checkpoint=Mock(side_effect=OSError(SECRET)), clock=lambda: NOW)
        create.assert_not_called()

    async def test_expiry_or_repeat_prevents_client_configuration(self):
        for record, now in ((check.new_record(), rf.PRICING_REVIEW_DEADLINE),
                            (check.new_record() | {"request_attempts": 1}, NOW)):
            client = Mock()
            with self.assertRaises(rf.ResearchBlocked):
                await check.run_probe(client, record, checkpoint=Mock(), clock=lambda: now)
            client.with_options.assert_not_called()


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
                       valid[:2] + ["invalid"], valid[:3] + ["M\tresearch_diagnostic_check.py"]):
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
        result = subprocess.run([sys.executable, "research_diagnostic_check.py"],
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
                with self.assertRaisesRegex(SystemExit, "did not complete"):
                    check.main()
            self.assertTrue(claim.exists())
            self.assertEqual(snapshots[-1]["reserved_usd"], "0.25")
            self.assertEqual(snapshots[-1]["request_attempts"], 0)
            self.assertIsNone(snapshots[-1]["estimated_cost_usd"])
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
        self.assertIn("check_research_diagnostic_sdk_offline.py", workflow)
        self.assertIn("poetry install --no-interaction --no-root --only main", workflow)
        self.assertLess(workflow.index("check_research_diagnostic_sdk_offline.py"), workflow.index("--execute"))
        if (root / check.TRIGGER_PATH).exists():
            self.assertEqual((root / check.TRIGGER_PATH).read_text(), check.TRIGGER_TEXT)


if __name__ == "__main__":
    unittest.main()
