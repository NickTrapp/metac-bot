import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

import public_smoke as smoke
from supportlab.llm import AgentConfig, FakeToolBackend, LLMAgent, Pricing


class PaidFake(FakeToolBackend):
    is_paid = True
    label = "offline-paid-contract-stub"
    def count_input_tokens(self, request):
        return 100
    def request(self, request):
        result = super().request(request)
        result.update(model=request["model"], usage={"input_tokens": 100, "output_tokens": 20})
        return result


class PublicSmokeTests(unittest.TestCase):
    def setUp(self):
        # Match the worker's explicitly scrubbed UTC environment.
        timezone_patch = patch.dict(os.environ, {"TZ": "UTC"})
        timezone_patch.start(); time.tzset()
        def restore_timezone():
            timezone_patch.stop(); time.tzset()
        self.addCleanup(restore_timezone)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        source = smoke.ROOT
        manifest = json.loads((source / "PUBLIC_SMOKE_MANIFEST.json").read_text())
        for entry in manifest["files"]:
            target = self.root / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / entry["path"], target)
        shutil.copyfile(source / "PUBLIC_SMOKE_MANIFEST.json", self.root / "PUBLIC_SMOKE_MANIFEST.json")
        self.root_patch = patch.object(smoke, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.socket_patch = patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)

    def factory(self, override=None):
        owner = self
        counts = []
        class Candidate:
            isolation = "offline-contract-test"
            def __init__(self, mode, backend, config, **kwargs):
                counts.append(mode)
                owner.assertEqual(backend, "openai")
                owner.assertEqual(kwargs["transport"], "stdlib")
                raw = copy.deepcopy(config)
                raw["pricing"] = Pricing(**raw["pricing"])
                raw["pricing"].validate(smoke.MODEL)
                owner.assertEqual(raw["approved_budget_usd"], "0.0625")
                self.agent = LLMAgent(PaidFake(), AgentConfig(**raw), mode)
                state = json.loads((owner.root/".work/public-smoke-live.json").read_text())
                owner.assertEqual(state["episodes"][-1]["status"], "in_flight")
            def decide(self, payload):
                return self.agent.decide(payload)
            @property
            def telemetry(self):
                result = self.agent.telemetry
                if override:
                    result.update(copy.deepcopy(override))
                return result
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return Candidate, counts

    def test_four_public_episodes_same_pinned_config(self):
        factory, counts = self.factory()
        result = smoke.run_smoke(True, {"pricing_checked_utc": datetime.now(timezone.utc).isoformat()}, factory)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(counts, ["direct", "procedure", "procedure", "direct"])
        self.assertEqual(len(result["episodes"]), 4)
        self.assertTrue(all(entry["passed"] for entry in result["episodes"]))
        self.assertLess(float(result["accounted_usd"]), .25)
        with self.assertRaises(FileExistsError):
            smoke.run_smoke(True, {"pricing_checked_utc": "unused"}, factory)
        self.assertEqual(len(counts), 4)

    def test_uncertain_episode_stops_with_full_reservation(self):
        factory, counts = self.factory({"uncertain_cost": True})
        result = smoke.run_smoke(True, {"pricing_checked_utc": "offline-test"}, factory)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(len(counts), 1)
        self.assertEqual(result["accounted_usd"], "0.0625")

    def test_wrong_model_stops_and_does_not_release_unknown_cost(self):
        factory, counts = self.factory({"requests": [{"model": "wrong-model"}]})
        result = smoke.run_smoke(True, {"pricing_checked_utc": "offline-test"}, factory)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(len(counts), 1)
        self.assertEqual(result["accounted_usd"], "0.0625")

    def test_contract_failure_stops_but_scored_action_failure_does_not(self):
        factory, counts = self.factory({"errors": ["api_contract_failure"]})
        result = smoke.run_smoke(True, {"pricing_checked_utc": "offline-test"}, factory)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(len(counts), 1)

    def test_scored_action_failure_remains_in_denominator(self):
        factory, counts = self.factory({"errors": ["malformed_response"]})
        result = smoke.run_smoke(True, {"pricing_checked_utc": "offline-test"}, factory)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(counts), 4)

    def test_manifest_change_or_private_material_blocks(self):
        (self.root/"supportlab/llm.py").write_text("altered")
        with self.assertRaisesRegex(ValueError, "source_manifest_mismatch"):
            smoke.run_smoke()

    def test_private_grader_presence_blocks(self):
        (self.root/"supportlab/bench/grading.py").write_text("unread")
        with self.assertRaisesRegex(ValueError, "private_evaluation_material_present"):
            smoke.run_smoke()

    def test_execute_without_context_never_reaches_transport(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(smoke, "run_smoke", side_effect=AssertionError("must not run")):
            self.assertEqual(smoke.main(["--execute"]), 2)

    def test_stdlib_worker_boots_without_sdk_or_any_decision_request(self):
        # The reviewed backend is lazy: startup does not issue a preflight or
        # generation request. Only a synthetic credential is supplied here.
        config = {"model":smoke.MODEL,"max_model_calls":4,"max_output_tokens":8192,
                  "max_input_tokens":16000,"timeout_seconds":120,"reasoning_effort":"xhigh",
                  "allow_paid":True,"approved_budget_usd":"0.0625",
                  "pricing":{"model":smoke.MODEL,"input_per_million":".75","output_per_million":"4.5",
                             "source_url":smoke.PRICING_URL,"verified_at":datetime.now(timezone.utc).date().isoformat(),
                             "cached_input_per_million":".075"}}
        with patch.dict(os.environ,{"OPENAI_API_KEY":"synthetic-offline-startup-only"}):
            with smoke.CandidateProcess("direct","openai",config,transport="stdlib") as candidate:
                self.assertEqual(candidate.isolation,"trusted-python-remote-model-only")
                self.assertEqual(candidate.telemetry,{})

    def admission_fixture(self):
        now = datetime.now(timezone.utc)
        head, parent = "a"*40, "b"*40
        env = {"GITHUB_REPOSITORY": smoke.REPOSITORY, "GITHUB_EVENT_NAME": "push",
               "GITHUB_REF": "refs/heads/"+smoke.BRANCH, "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "1",
               "GITHUB_WORKFLOW_REF": f"{smoke.REPOSITORY}/.github/workflows/{smoke.WORKFLOW}@refs/heads/{smoke.BRANCH}",
               "GITHUB_SHA": head, "GITHUB_RUN_ID": "123"}
        admission = {"run_id": smoke.RUN_ID, "scope": "public_golden_smoke", "execution_admitted": True,
                     "prepared_commit": parent, "stage_cap_usd": ".25", "model": smoke.MODEL,
                     "manifest_sha256": smoke.digest(self.root/"PUBLIC_SMOKE_MANIFEST.json"),
                     "pricing_checked_utc": (now-timedelta(minutes=1)).isoformat(),
                     "expires_at_utc": (now+timedelta(minutes=10)).isoformat(),
                     "shared_reservation_receipt_sha256": "c"*64}
        admission["stage_cap_usd"] = "0.25"
        target=self.root/smoke.TRIGGER_PATH;target.parent.mkdir(parents=True);target.write_text(json.dumps(admission))
        history={"total_count":1,"workflow_runs":[{"id":123,"head_sha":head,"head_branch":smoke.BRANCH,"event":"push",
                  "path":".github/workflows/"+smoke.WORKFLOW,"run_number":1,"run_attempt":1,"status":"in_progress","conclusion":None}]}
        mapping={('rev-parse','HEAD'):head,('rev-parse','HEAD^'):parent,
                 ('diff-tree','--no-commit-id','--name-status','-r','HEAD'):'A\t'+smoke.TRIGGER_PATH,
                 ('status','--porcelain','--untracked-files=no'):'',('rev-parse','--show-toplevel'):str(self.root)}
        def git(args,**kwargs):return mapping[tuple(args[1:])]
        return env,history,git

    def test_exact_trigger_first_run_and_history_required(self):
        env,history,git=self.admission_fixture()
        with patch.object(smoke.subprocess,'check_output',side_effect=git):
            self.assertTrue(smoke.check_admission(env,lambda e:history)['execution_admitted'])
            with self.assertRaises(ValueError):
                smoke.check_admission(env|{'GITHUB_RUN_ATTEMPT':'2'},lambda e:history)
            with self.assertRaises(ValueError):
                smoke.check_admission(env,lambda e:history|{'total_count':2})


if __name__ == "__main__":
    unittest.main()
