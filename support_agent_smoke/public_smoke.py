"""Public G01/G02 paired smoke. Default is offline; live admission is separate.

No private fixtures or grader imports. The live transport adaptation is explicit.
"""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile

from supportlab.bench.process import CandidateProcess
from supportlab.simulator import Simulator

ROOT = Path(__file__).resolve().parent
RUN_ID = "sal-20261015-v02"
MODEL = "gpt-5.4-mini-2026-03-17"
REPOSITORY = "NickTrapp/metac-bot"
BRANCH = "support-agent-experiment-20261015"
WORKFLOW = "support_agent_smoke_20261015.yaml"
TRIGGER_PATH = ".github/support-agent-smoke-20261015.trigger"
STAGE_CAP = Decimal("0.25")
EPISODE_CAP = STAGE_CAP / 4
ORIGINAL_SOURCE_COMMIT = "672fc801df52d85fc6e681601b737ceae5ac9fd5"
PRICING_URL = "https://developers.openai.com/api/docs/models/gpt-5.4-mini"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_public_sources():
    manifest = json.loads((ROOT / "PUBLIC_SMOKE_MANIFEST.json").read_text())
    if manifest.get("run_id") != RUN_ID:
        raise ValueError("source_manifest_run_mismatch")
    for entry in manifest["files"]:
        path = Path(entry["path"])
        if path.is_absolute() or ".." in path.parts or digest(ROOT / path) != entry["sha256"]:
            raise ValueError("source_manifest_mismatch")
    for private in ("fixtures.py", "grading.py", "data/test.json", "data/train.json", "data/dev.json"):
        if (ROOT / "supportlab/bench" / private).exists():
            raise ValueError("private_evaluation_material_present")
    return manifest


def read_current_history(env):
    token = env.get("GH_TOKEN")
    if not token:
        raise ValueError("history_access_missing")
    connection = http.client.HTTPSConnection("api.github.com", timeout=20)
    try:
        connection.request("GET", f"/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs?per_page=100",
                           headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                    "User-Agent": "support-smoke-admission", "X-GitHub-Api-Version": "2022-11-28"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("history_unavailable")
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("history_oversized")
        return json.loads(raw)
    except Exception:
        raise ValueError("history_unavailable") from None
    finally:
        connection.close()


def check_admission(env, history_reader=read_current_history):
    expected = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF": "refs/heads/" + BRANCH, "GITHUB_RUN_NUMBER": "1", "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/{BRANCH}"}
    if any(env.get(key) != value for key, value in expected.items()):
        raise ValueError("live_context_blocked")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    head, parent = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^")
    if head != env.get("GITHUB_SHA") or git("diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD") != "A\t" + TRIGGER_PATH:
        raise ValueError("trigger_must_be_separate_addition_only")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("tracked_source_modified")
    repository_root = Path(git("rev-parse", "--show-toplevel"))
    admission = json.loads((repository_root / TRIGGER_PATH).read_text())
    required = {"run_id": RUN_ID, "scope": "public_golden_smoke", "execution_admitted": True,
                "prepared_commit": parent, "stage_cap_usd": "0.25", "model": MODEL,
                "manifest_sha256": digest(ROOT / "PUBLIC_SMOKE_MANIFEST.json")}
    if any(admission.get(key) != value for key, value in required.items()):
        raise ValueError("admission_record_mismatch")
    now = datetime.now(timezone.utc)
    checked = datetime.fromisoformat(admission["pricing_checked_utc"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(admission["expires_at_utc"].replace("Z", "+00:00"))
    if checked.utcoffset() is None or expires.utcoffset() is None or not checked <= now < expires or (expires-checked).total_seconds() > 86400:
        raise ValueError("admission_or_pricing_expired")
    if not re.fullmatch(r"[0-9a-f]{64}", admission.get("shared_reservation_receipt_sha256", "")):
        raise ValueError("shared_reservation_attestation_missing")
    history = history_reader(env)
    runs = history.get("workflow_runs", [])
    if history.get("total_count") != 1 or len(runs) != 1:
        raise ValueError("workflow_already_attempted_or_history_incomplete")
    run = runs[0]
    expected_run = {"head_sha": head, "head_branch": BRANCH, "event": "push",
                    "path": ".github/workflows/" + WORKFLOW, "run_number": 1, "run_attempt": 1,
                    "status": "in_progress", "conclusion": None}
    if str(run.get("id")) != env.get("GITHUB_RUN_ID") or any(run.get(k) != v for k, v in expected_run.items()):
        raise ValueError("workflow_history_mismatch")
    return admission


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".smoke-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2, allow_nan=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def public_checks(actual, expected):
    refunds = [{k: value[k] for k in ("order_id", "amount_cents", "reason")} for value in actual["refunds"]]
    return {"orders": actual["orders"] == expected["orders"], "refunds": refunds == expected["refunds"],
            "escalations": actual["escalations"] == expected["escalations"], "outcome": actual["outcome"] == expected["outcome"],
            "clarifications": actual["clarifications"] >= expected["min_clarifications"]}


def run_smoke(execute=False, admission=None, candidate_factory=CandidateProcess):
    verify_public_sources()
    if execute and not admission:
        raise ValueError("live_admission_missing")
    cases = json.loads((ROOT / "supportlab/bench/data/public-smoke.json").read_text())
    if [case["case_id"] for case in cases] != ["G01", "G02"] or any(len(case["turns"]) != 1 for case in cases):
        raise ValueError("public_smoke_scope_changed")
    state = {"run_id": RUN_ID, "scope": "public_golden_smoke", "original_source_commit": ORIGINAL_SOURCE_COMMIT,
             "adaptation": "reviewed safety fixes plus explicit no-install stdlib HTTPS transport",
             "sdk_version": None, "python_version": platform.python_version(),
             "backend": "openai-responses-stdlib-https" if execute else "fake", "model": MODEL if execute else "fake-tool-v1",
             "status": "running", "stage_cap_usd": "0.25", "accounted_usd": "0.00", "episodes": [],
             "started_at_utc": datetime.now(timezone.utc).isoformat(), "manifest_sha256": digest(ROOT / "PUBLIC_SMOKE_MANIFEST.json"),
             "pricing_source": PRICING_URL, "generation_bound_usd": "0.195456", "generation_bound_calls": 4,
             "claim": "public synthetic smoke only; not the private held-out comparison"}
    path = ROOT / ".work" / ("public-smoke-live.json" if execute else "public-smoke-offline.json")
    path.parent.mkdir(exist_ok=True)
    if execute:
        with path.open("x") as stream:
            json.dump(state, stream); stream.flush(); os.fsync(stream.fileno())
    else:
        save_state(path, state)
    config = {"model": MODEL if execute else "fake-tool-v1", "max_model_calls": 4, "max_output_tokens": 8192,
              "max_input_tokens": 16000, "timeout_seconds": 120, "reasoning_effort": "xhigh"}
    if execute:
        config.update(allow_paid=True, approved_budget_usd=str(EPISODE_CAP),
                      pricing={"model": MODEL, "input_per_million": ".75", "output_per_million": "4.5",
                               "source_url": PRICING_URL, "verified_at": datetime.now(timezone.utc).date().isoformat(), "cached_input_per_million": ".075"})
        state["pricing_checked_utc"] = admission["pricing_checked_utc"]
    for index, case in enumerate(cases):
        for mode in (["direct", "procedure"] if index % 2 == 0 else ["procedure", "direct"]):
            entry = {"case_id": case["case_id"], "mode": mode, "status": "in_flight",
                     "reserved_usd": str(EPISODE_CAP if execute else Decimal(0))}
            state["episodes"].append(entry)
            state["accounted_usd"] = str(Decimal(state["accounted_usd"]) + (EPISODE_CAP if execute else Decimal(0)))
            save_state(path, state)  # Reservation is durable before candidate/network use.
            telemetry = {}; error = False
            try:
                simulator = Simulator(case)
                with candidate_factory(mode, "openai" if execute else "fake", config, timeout_seconds=130,
                                       transport="stdlib" if execute else "sdk") as candidate:
                    for turn in case["turns"]:
                        action = candidate.decide(simulator.payload(turn))
                        telemetry = candidate.telemetry
                        simulator.step(turn, action)
                    entry["isolation"] = candidate.isolation
                entry["checks"] = public_checks(simulator.snapshot(), case["expected"])
                entry["passed"] = all(entry["checks"].values())
            except Exception:
                error = True
                entry["worker_error"] = "worker_or_contract_failure"
            entry["telemetry"] = telemetry
            uncertain = bool(error or telemetry.get("uncertain_cost") or "budget_accounted_usd" not in telemetry)
            stop_reason = telemetry.get("stop_reason")
            if "api_contract_failure" in telemetry.get("errors", []):
                stop_reason = stop_reason or "api_contract_failure"
            if execute and any(request.get("model") != MODEL for request in telemetry.get("requests", []) if "model" in request):
                stop_reason = "model_identity_mismatch"; uncertain = True
            if execute:
                accounted = EPISODE_CAP if uncertain else Decimal(str(telemetry["budget_accounted_usd"]))
                if not accounted.is_finite() or accounted < 0:
                    accounted = EPISODE_CAP; uncertain = True
                state["accounted_usd"] = str(Decimal(state["accounted_usd"]) - EPISODE_CAP + accounted)
                if accounted > EPISODE_CAP:
                    stop_reason = "accounting_overrun"
                entry["accounted_usd"] = str(accounted)
            entry["status"] = "stopped" if error or execute and (uncertain or stop_reason) else "finished"
            if entry["status"] == "stopped":
                entry["stop_reason"] = stop_reason or "unresolved_billing_or_worker_failure"
                state["status"] = "stopped"; save_state(path, state)
                return state
            save_state(path, state)
    state["status"] = "completed"
    state["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    save_state(path, state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        verify_public_sources()
        admission = check_admission(os.environ) if args.execute else None
        state = run_smoke(args.execute, admission)
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "admission_or_execution_check_failed", "paid_execution_retried": False}))
        return 2
    print(json.dumps({"status": state["status"], "episodes": len(state["episodes"]),
                      "passed_episodes": sum(bool(e.get("passed")) for e in state["episodes"]),
                      "backend": state["backend"], "accounted_usd": state["accounted_usd"]}, sort_keys=True))
    return 0 if state["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
