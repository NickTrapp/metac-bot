import copy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import research_integration_check as check
from researched_forecast import ResearchBlocked


SHA = "a" * 40
ENV = {"GITHUB_REPOSITORY": "NickTrapp/metac-bot", "GITHUB_EVENT_NAME": "push",
       "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_RUN_ID": "123", "GITHUB_SHA": SHA, "RESEARCH_CHECK_APPROVED": "true",
       "GITHUB_WORKFLOW_REF": "NickTrapp/metac-bot/.github/workflows/research_integration_check.yaml@refs/heads/main"}
HISTORY = {"total_count": 1, "workflow_runs": [{"id": 123, "head_sha": SHA,
    "head_branch": "main", "event": "push", "path": ".github/workflows/research_integration_check.yaml",
    "run_number": 1, "run_attempt": 1, "status": "in_progress", "conclusion": None}]}


class GateTests(unittest.TestCase):
    def test_exact_context_and_history(self):
        check.verify_context(ENV)
        check.verify_history(ENV, HISTORY)

    def test_each_context_change_blocks(self):
        for key in ENV:
            if key == "GITHUB_RUN_ID":
                continue
            with self.subTest(key=key), self.assertRaises(ResearchBlocked):
                check.verify_context(ENV | {key: "unexpected"})

    def test_changed_missing_duplicate_history_blocks(self):
        for value in ({}, {"total_count": 2, "workflow_runs": HISTORY["workflow_runs"] * 2},
                      {"total_count": 1, "workflow_runs": []}):
            with self.assertRaises(ResearchBlocked):
                check.verify_history(ENV, value)
        for key in HISTORY["workflow_runs"][0]:
            value = copy.deepcopy(HISTORY)
            value["workflow_runs"][0][key] = "wrong"
            with self.subTest(key=key), self.assertRaises(ResearchBlocked):
                check.verify_history(ENV, value)

    def test_trigger_only_commit_and_fixed_marker_required(self):
        with patch.object(check.subprocess, "check_output", side_effect=[SHA, "A\t" + check.TRIGGER_PATH]), \
                patch.object(Path, "read_text", return_value=check.TRIGGER_TEXT):
            check.verify_trigger_commit(ENV)
        for output in (["b" * 40], [SHA, "M\t" + check.TRIGGER_PATH],
                       [SHA, "A\t" + check.TRIGGER_PATH + "\nM\tresearched_forecast.py"]):
            with patch.object(check.subprocess, "check_output", side_effect=output), self.assertRaises(ResearchBlocked):
                check.verify_trigger_commit(ENV)
        with patch.object(check.subprocess, "check_output", side_effect=[SHA, "A\t" + check.TRIGGER_PATH]), \
                patch.object(Path, "read_text", return_value="wrong"), self.assertRaises(ResearchBlocked):
            check.verify_trigger_commit(ENV)

    def test_invalid_context_does_not_read_github(self):
        with patch.object(check, "read_actions_json") as read, \
                patch.object(check, "verify_trigger_commit") as trigger:
            with self.assertRaises(ResearchBlocked):
                check.check_gate({})
            read.assert_not_called()
            trigger.assert_not_called()

    def test_gate_reads_exact_workflow(self):
        with patch.object(check, "read_actions_json", return_value=HISTORY) as read, \
                patch.object(check, "verify_trigger_commit"):
            check.check_gate(ENV)
        read.assert_called_once_with(ENV, "workflows/research_integration_check.yaml/runs?per_page=100")


class OutputTests(unittest.TestCase):
    def test_summary_retains_partial_cost_without_research_text(self):
        record = {"operational_status": "incomplete", "request_attempts": 2,
                  "reserved_usd": "0.25", "research": {"text": "do not log this text",
                  "citations": [{"url": "https://www.nasa.gov/history/apollo-11/"}]},
                  "usage_observations": [{"estimated_cost_usd": "0.014"}]}
        summary = check.public_summary(record)
        self.assertEqual(summary["known_estimated_cost_usd"], "0.014")
        self.assertFalse(summary["usage_complete"])
        self.assertTrue(summary["nasa_citation_present"])
        self.assertNotIn("do not log", json.dumps(summary))
        self.assertEqual(summary["forecast_skill"], "not_evaluated")

    def test_fake_nasa_host_is_not_accepted(self):
        for url in ("https://nasa.gov.example.org/", "https://notnasa.gov/"):
            summary = check.public_summary({"research": {"citations": [{"url": url}]}})
            self.assertFalse(summary["nasa_citation_present"])

    def test_known_yes_label_is_only_an_operational_sanity_check(self):
        for probability in (0, 0.5, False, None, 2):
            self.assertFalse(check.public_summary({"forecast": {"probability_yes": probability}})["known_label_agrees"])
        self.assertTrue(check.public_summary({"forecast": {"probability_yes": 0.99}})["known_label_agrees"])

    def test_cli_default_is_offline(self):
        result = subprocess.run([sys.executable, "research_integration_check.py"],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Offline only", result.stdout)

    def test_paid_gate_fails_before_client_import_or_output(self):
        with patch.object(sys, "argv", ["check", "--execute"]), \
                patch.object(check, "check_gate", side_effect=RuntimeError("raw exception")), \
                patch.object(check, "run_check") as run, \
                patch.object(Path, "write_text") as write:
            with self.assertRaisesRegex(SystemExit, "gate blocked") as error:
                check.main()
            self.assertNotIn("raw exception", str(error.exception))
            run.assert_not_called()
            write.assert_not_called()

    def test_workflow_one_shot_without_metaculus_or_trigger_in_patch(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/research_integration_check.yaml").read_text()
        self.assertIn("paths: [.github/research-integration-20261010.trigger]", workflow)
        self.assertIn("github.run_number == 1 && github.run_attempt == 1", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("METACULUS_TOKEN", workflow)
        self.assertEqual(workflow.count("secrets."), 1)
        self.assertIn("secrets.OPENAI_API_KEY", workflow)
        self.assertIn("check_research_sdk_offline.py", workflow)
        self.assertIn("path: research-check-result.json", workflow)
        # If present in the separately commissioned trigger commit, it must
        # still be the fixed marker. Offline tests must pass on that commit too.
        if (root / check.TRIGGER_PATH).exists():
            self.assertEqual((root / check.TRIGGER_PATH).read_text(), check.TRIGGER_TEXT)


if __name__ == "__main__":
    unittest.main()
