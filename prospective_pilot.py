"""One-shot public prospective pilot; default and validation modes are offline."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import hashlib
import logging
import os
from pathlib import Path
import subprocess

import prospective_forecast as pf
from researched_forecast import API_BASE, ResearchBlocked
from safe_smoke_test import REPOSITORY, read_actions_json

WORKFLOW = "prospective_pilot_20261010.yaml"
MANIFEST_PATH = Path("prospective-manifest-20261010.json")
EXPECTED_MANIFEST_FILE_SHA256 = "a4ad0deeacd8069ec2d3f615aa4a917e4d1418a62ee1ad6014d55ae132aba29d"
TRIGGER_PATH = ".github/prospective-pilot-20261010.trigger"
TRIGGER_TEXT = pf.PILOT_ID + "\n"
ARTIFACT_PATH = Path("prospective-pilot-result.json")
CLAIM_PATH = Path(".prospective-pilot-20261010.claim")


def verify_context(env: dict[str, str]) -> None:
    expected = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1",
                "GITHUB_RUN_ATTEMPT": "1", "PROSPECTIVE_PILOT_APPROVED": "true",
                "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/main"}
    if any(env.get(key) != value for key, value in expected.items()):
        raise ResearchBlocked("This is not the approved first prospective-pilot push and attempt.")
    sha = env.get("GITHUB_SHA", "")
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ResearchBlocked("Pilot source identity is unavailable.")


def verify_trigger_commit(env: dict[str, str]) -> str:
    def git(*args):
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    try:
        if git("rev-parse", "HEAD") != env["GITHUB_SHA"]:
            raise ValueError()
        if git("diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD") != "A\t" + TRIGGER_PATH:
            raise ValueError()
        if Path(TRIGGER_PATH).read_text() != TRIGGER_TEXT:
            raise ValueError()
        source_commit = git("rev-parse", "HEAD^")
        if len(source_commit) != 40:
            raise ValueError()
        # Prevent a manifest edited after freezing or checkout tampering.
        if git("status", "--porcelain", "--untracked-files=no"):
            raise ValueError()
    except Exception:
        raise ResearchBlocked("Pilot trigger must be a separate fixed trigger-only commit on a clean checkout.") from None
    return source_commit


def verify_history(env: dict[str, str], data: dict) -> None:
    runs = data.get("workflow_runs")
    if data.get("total_count") != 1 or not isinstance(runs, list) or len(runs) != 1:
        raise ResearchBlocked("Pilot history is incomplete or already attempted.")
    run = runs[0]
    expected = {"head_sha": env["GITHUB_SHA"], "head_branch": "main", "event": "push",
                "path": f".github/workflows/{WORKFLOW}", "run_number": 1, "run_attempt": 1,
                "status": "in_progress", "conclusion": None}
    if (not isinstance(run, dict) or str(run.get("id")) != env.get("GITHUB_RUN_ID")
            or any(run.get(key) != value for key, value in expected.items())):
        raise ResearchBlocked("Cannot verify the sole pilot run for this commit.")


def check_gate(env: dict[str, str]) -> str:
    verify_context(env)
    source_commit = verify_trigger_commit(env)
    verify_history(env, read_actions_json(env, f"workflows/{WORKFLOW}/runs?per_page=100"))
    return source_commit


def read_manifest(path: Path, *, expected_sha: str | None = None) -> dict:
    if path.stat().st_size > 128 * 1024:
        raise ValueError("Manifest exceeds the reviewed bound.")
    data = path.read_bytes()
    if expected_sha is not None and hashlib.sha256(data).hexdigest() != expected_sha:
        raise ValueError("Frozen manifest file hash differs from the reviewed selection.")
    return json.loads(data.decode("utf-8"))


def save_checkpoint(record: dict) -> None:
    temp = ARTIFACT_PATH.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, ARTIFACT_PATH)


def summary(record: dict) -> dict:
    records = record["records"]
    return {"pilot_id": record["pilot_id"], "manifest_sha256": record["manifest_sha256"],
            "protocol_sha256": record["protocol_sha256"], "run_status": record["run_status"],
            "frozen_questions": len(records),
            "successful_pairs": sum(r["pair_valid"] and r["baseline"]["status"] == "completed"
                                    and r["researched"]["status"] == "completed" for r in records),
            "failed_arms": sum(r[a]["status"] != "completed" for r in records
                               for a in ("baseline", "researched")),
            "request_attempts": record["request_attempts"],
            "search_allowance_consumed": record["search_allowance_consumed"],
            "observed_search_calls": record["observed_search_calls"],
            "reserved_usd": record["reserved_usd"],
            "known_estimated_cost_usd": record["known_estimated_cost_usd"],
            "usage_complete": record["usage_complete"], "forecast_skill": "not_evaluated"}


async def run_once(manifest: dict, *, env: dict, source_commit: str) -> dict:
    # Exclusive same-workspace claim complements the durable sole Actions run.
    # Never delete/refund this claim after failure, even before the first request.
    with CLAIM_PATH.open("x", encoding="utf-8") as claim:
        json.dump({"run_id": env["GITHUB_RUN_ID"], "source_commit": source_commit,
                   "reserved_usd": str(pf.RESERVATION_USD)}, claim)
        claim.flush()
        os.fsync(claim.fileno())
    from openai import AsyncOpenAI
    reserved = False

    def reserve(amount):
        nonlocal reserved
        if reserved or amount != pf.RESERVATION_USD:
            raise ResearchBlocked("Pilot reservation already consumed or changed.")
        reserved = True

    def checkpoint(record):
        record["manifest_file_sha256"] = EXPECTED_MANIFEST_FILE_SHA256
        record["source_commit"] = source_commit
        record["trigger_commit"] = env["GITHUB_SHA"]
        record["github_run_id"] = env["GITHUB_RUN_ID"]
        save_checkpoint(record)

    async with AsyncOpenAI(base_url=API_BASE, max_retries=0, timeout=90) as client:
        return await pf.run_pilot(manifest, client, reserve=reserve, checkpoint=checkpoint)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--validate-manifest", action="store_true")
    modes.add_argument("--check-gate", action="store_true")
    modes.add_argument("--execute", action="store_true")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    args = parser.parse_args()
    if not any((args.validate_manifest, args.check_gate, args.execute)):
        print("Offline only. See PROSPECTIVE_PILOT.md.")
        return
    logging.disable(logging.CRITICAL)
    try:
        if args.validate_manifest:
            manifest = pf.normalize_manifest(read_manifest(args.manifest), now=datetime.now(timezone.utc))
            print(json.dumps({"questions": len(manifest["questions"]), "manifest_sha256": pf.sha256(manifest),
                              "protocol_sha256": pf.sha256(pf.PROTOCOL)}))
            return
        if args.manifest != MANIFEST_PATH:
            raise ValueError("Paid execution requires the frozen committed manifest path.")
        env = dict(os.environ)
        source_commit = check_gate(env)
        manifest = pf.normalize_manifest(read_manifest(MANIFEST_PATH, expected_sha=EXPECTED_MANIFEST_FILE_SHA256),
                                         now=datetime.now(timezone.utc))
    except Exception:
        raise SystemExit("Prospective pilot gate blocked; no paid request sent.") from None
    if args.check_gate:
        print("One-shot pilot gate and frozen manifest verified.")
        return
    try:
        record = asyncio.run(run_once(manifest, env=env, source_commit=source_commit))
    except Exception:
        raise SystemExit("Pilot stopped. Retain existing records and reservation; do not retry or replace questions.") from None
    print(json.dumps(summary(record), sort_keys=True))
    # Failure is a retained observation, not permission to repair and rerun.
    if summary(record)["successful_pairs"] != len(record["records"]):
        raise SystemExit("Pilot retained incomplete pairs; no retry or replacement.")


if __name__ == "__main__":
    main()
