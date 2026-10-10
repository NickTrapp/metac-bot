import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import prospective_pilot as pilot
from researched_forecast import ResearchBlocked
from test_prospective_forecast import manifest, NOW

SHA, SOURCE_SHA = "a" * 40, "b" * 40
ENV = {"GITHUB_REPOSITORY": "NickTrapp/metac-bot", "GITHUB_EVENT_NAME": "push",
       "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "1",
       "GITHUB_RUN_ID": "123", "GITHUB_SHA": SHA, "PROSPECTIVE_PILOT_APPROVED": "true",
       "GITHUB_WORKFLOW_REF": "NickTrapp/metac-bot/.github/workflows/prospective_pilot_20261010.yaml@refs/heads/main"}
HISTORY = {"total_count": 1, "workflow_runs": [{"id": 123, "head_sha": SHA, "head_branch": "main",
    "event": "push", "path": ".github/workflows/prospective_pilot_20261010.yaml", "run_number": 1,
    "run_attempt": 1, "status": "in_progress", "conclusion": None}]}


class GateTests(unittest.TestCase):
    def test_exact_context_and_history(self):
        pilot.verify_context(ENV); pilot.verify_history(ENV, HISTORY)

    def test_each_context_change_blocks(self):
        for key in ENV:
            if key != "GITHUB_RUN_ID":
                with self.subTest(key=key), self.assertRaises(ResearchBlocked):
                    pilot.verify_context(ENV | {key: "wrong"})

    def test_changed_duplicate_or_incomplete_history_blocks(self):
        for data in ({}, {"total_count": 2, "workflow_runs": HISTORY["workflow_runs"] * 2},
                     {"total_count": 1, "workflow_runs": []}):
            with self.assertRaises(ResearchBlocked):
                pilot.verify_history(ENV, data)
        for key in HISTORY["workflow_runs"][0]:
            data = copy.deepcopy(HISTORY); data["workflow_runs"][0][key] = "wrong"
            with self.subTest(key=key), self.assertRaises(ResearchBlocked):
                pilot.verify_history(ENV, data)

    def test_trigger_only_fixed_marker_and_clean_checkout(self):
        outputs = [SHA, "A\t" + pilot.TRIGGER_PATH, SOURCE_SHA, ""]
        with patch.object(pilot.subprocess, "check_output", side_effect=outputs), \
                patch.object(Path, "read_text", return_value=pilot.TRIGGER_TEXT):
            self.assertEqual(pilot.verify_trigger_commit(ENV), SOURCE_SHA)
        for value in (["c" * 40], [SHA, "M\t" + pilot.TRIGGER_PATH],
                      [SHA, "A\t" + pilot.TRIGGER_PATH + "\nM\tprospective_forecast.py"],
                      [SHA, "A\t" + pilot.TRIGGER_PATH, SOURCE_SHA, " M prospective_pilot.py"]):
            with patch.object(pilot.subprocess, "check_output", side_effect=value), \
                    patch.object(Path, "read_text", return_value=pilot.TRIGGER_TEXT), self.assertRaises(ResearchBlocked):
                pilot.verify_trigger_commit(ENV)
        with patch.object(pilot.subprocess, "check_output", side_effect=outputs), \
                patch.object(Path, "read_text", return_value="changed"), self.assertRaises(ResearchBlocked):
            pilot.verify_trigger_commit(ENV)

    def test_invalid_context_cannot_read_github(self):
        with patch.object(pilot, "read_actions_json") as read, self.assertRaises(ResearchBlocked):
            pilot.check_gate({})
        read.assert_not_called()

    def test_manifest_hash_pin_and_input_bound(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "manifest.json"
            path.write_text(json.dumps(manifest()))
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(pilot.read_manifest(path, expected_sha=expected), manifest())
            path.write_text(json.dumps(manifest(4)))
            with self.assertRaisesRegex(ValueError, "hash"):
                pilot.read_manifest(path, expected_sha=expected)
            path.write_text("x" * (128 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, "bound"):
                pilot.read_manifest(path)

    def test_committed_manifest_is_exact_reviewed_file(self):
        root = Path(__file__).resolve().parents[1]
        data = pilot.read_manifest(root / pilot.MANIFEST_PATH, expected_sha=pilot.EXPECTED_MANIFEST_FILE_SHA256)
        self.assertEqual(len(data["questions"]), 5)

    def test_save_checkpoint_atomic_and_strict_json(self):
        with tempfile.TemporaryDirectory() as root, patch.object(pilot, "ARTIFACT_PATH", Path(root) / "result.json"):
            pilot.save_checkpoint({"status": "first"})
            self.assertEqual(json.loads(pilot.ARTIFACT_PATH.read_text()), {"status": "first"})
            pilot.save_checkpoint({"status": "second"})
            self.assertEqual(json.loads(pilot.ARTIFACT_PATH.read_text()), {"status": "second"})
            self.assertFalse(pilot.ARTIFACT_PATH.with_suffix(".tmp").exists())

    def test_default_cli_offline(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, "prospective_pilot.py"], cwd=root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Offline only", result.stdout)

    def test_paid_gate_fails_before_client_or_claim(self):
        with patch.object(sys, "argv", ["pilot", "--execute"]), \
                patch.object(pilot, "check_gate", side_effect=RuntimeError("private provider data")), \
                patch.object(pilot, "run_once") as run, patch.object(pilot, "read_manifest") as read:
            with self.assertRaisesRegex(SystemExit, "gate blocked") as error:
                pilot.main()
            self.assertNotIn("private", str(error.exception)); run.assert_not_called(); read.assert_not_called()

    def test_paid_path_cannot_select_arbitrary_manifest(self):
        with patch.object(sys, "argv", ["pilot", "--execute", "--manifest", "other.json"]), \
                patch.object(pilot, "check_gate") as gate, self.assertRaises(SystemExit):
            pilot.main()
        gate.assert_not_called()

    def test_workflow_is_one_shot_and_only_openai_secret(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows" / pilot.WORKFLOW).read_text()
        self.assertIn("paths: [.github/prospective-pilot-20261010.trigger]", workflow)
        self.assertIn("github.run_number == 1 && github.run_attempt == 1", workflow)
        self.assertNotIn("workflow_dispatch:", workflow); self.assertNotIn("schedule:", workflow)
        self.assertNotIn("METACULUS_TOKEN", workflow); self.assertEqual(workflow.count("secrets."), 1)
        self.assertIn("check_prospective_sdk_offline.py", workflow)
        self.assertIn("path: prospective-pilot-result.json", workflow)
        if (root / pilot.TRIGGER_PATH).exists():
            self.assertEqual((root / pilot.TRIGGER_PATH).read_text(), pilot.TRIGGER_TEXT)


if __name__ == "__main__":
    unittest.main()
