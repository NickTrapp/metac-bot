import unittest
import io
from pathlib import Path
import tempfile
from unittest.mock import patch
import zipfile
from decimal import Decimal
import research_loop as loop
from test_research_loop import manifest, NOW
from research_loop import LoopBlocked
import research_loop_actions as actions


class RestoreTests(unittest.TestCase):
    def history(self, previous=True):
        runs = [{"id": 2, "created_at": "2026-10-12T04:00:00Z", "status": "in_progress"}]
        if previous:
            runs.append({"id": 1, "created_at": "2026-10-12T03:00:00Z", "status": "completed"})
        return {"total_count": len(runs), "workflow_runs": runs}

    def test_first_run_and_resume(self):
        env = {"GITHUB_RUN_ID": "2", "GITHUB_RUN_ATTEMPT": "1"}
        self.assertIsNone(actions.select_previous(self.history(False), env))
        self.assertEqual(actions.select_previous(self.history(), env)["id"], 1)
        env["GITHUB_RUN_ATTEMPT"] = "2"
        self.assertEqual(actions.select_previous(self.history(), env)["id"], 2)

    def test_same_second_dispatch_still_finds_predecessor(self):
        data = self.history()
        data["workflow_runs"][1]["created_at"] = data["workflow_runs"][0]["created_at"]
        self.assertEqual(actions.select_previous(data, {"GITHUB_RUN_ID": "2", "GITHUB_RUN_ATTEMPT": "1"})["id"], 1)

    def test_missing_history_or_nonterminal_predecessor_blocks(self):
        env = {"GITHUB_RUN_ID": "2", "GITHUB_RUN_ATTEMPT": "1"}
        data = self.history()
        data["total_count"] = 3
        with self.assertRaisesRegex(LoopBlocked, "history_incomplete"):
            actions.select_previous(data, env)
        data = self.history()
        data["workflow_runs"][1]["status"] = "in_progress"
        with self.assertRaisesRegex(LoopBlocked, "not_terminal"):
            actions.select_previous(data, env)

    def test_expired_missing_and_duplicate_artifacts_never_reset_allowance(self):
        item = {"id": 1, "name": actions.ARTIFACT, "expired": False}
        self.assertEqual(actions.select_artifact({"total_count": 1, "artifacts": [item]})["id"], 1)
        for items in ([], [item | {"expired": True}], [item, item | {"id": 2}]):
            with self.assertRaisesRegex(LoopBlocked, "missing_or_ambiguous"):
                actions.select_artifact({"total_count": len(items), "artifacts": items})

    def test_restore_preserves_an_in_flight_attempt_from_zip(self):
        env = {"GITHUB_REPOSITORY": actions.REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
               "GITHUB_RUN_ID": "2", "GITHUB_RUN_ATTEMPT": "1"}
        with tempfile.TemporaryDirectory() as temp:
            original, target = Path(temp) / "original.sqlite", Path(temp) / "restored.sqlite"
            ledger = loop.Ledger(original, manifest(), Decimal("1"))
            q = manifest()["questions"][0]
            ledger.reserve(q["question_id"], "baseline", loop.build_request("baseline", q, NOW.isoformat()), NOW.isoformat())
            ledger.close()
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                archive.write(original, "research-loop.sqlite")
                archive.writestr("../ignored-file", "not extracted")
            artifacts = {"total_count": 1, "artifacts": [{"id": 10, "name": actions.ARTIFACT, "expired": False}]}
            with patch.object(actions, "TARGET", target), patch.object(actions, "gh_api", side_effect=[self.history(), artifacts, data.getvalue()]):
                self.assertEqual(actions.restore(env), "restored_previous_ledger")
            restored = loop.Ledger(target, manifest(), Decimal("1"))
            try:
                self.assertEqual(restored.attempt(q["question_id"], "baseline")["status"], "in_flight")
                with self.assertRaisesRegex(LoopBlocked, "prior_failed_or_ambiguous"):
                    restored.check_remaining_budget()
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
