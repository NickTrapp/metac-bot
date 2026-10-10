"""One resolved Apollo research request; no forecast, pilot rerun, or submission.

Default execution is offline. A new trigger-only commit and the sole first
Actions run are required before using the existing OpenAI Actions secret.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
from typing import Callable

import prospective_forecast as pf
import researched_forecast as rf
from research_integration_check import FIXTURE
from safe_smoke_test import REPOSITORY, read_actions_json

WORKFLOW = "research_diagnostic_20261010.yaml"
TRIGGER_PATH = ".github/research-diagnostic-20261010.trigger"
TRIGGER_TEXT = "apollo-primary-domain-research-diagnostic-v1\n"
ARTIFACT_PATH = Path("research-diagnostic-result.json")
CLAIM_PATH = Path(".research-diagnostic-20261010.claim")
# One request and at most two search calls. This reservation is not a bill cap.
RESERVATION_USD = "0.25"
QUESTION = {
    "title": FIXTURE.title,
    "description": FIXTURE.description,
    "resolution_criteria": FIXTURE.resolution_criteria,
    "fine_print": FIXTURE.fine_print,
    "event_time_utc": "1969-07-20T20:17:40+00:00",
    "resolution_source_url": "https://www.nasa.gov/mission/apollo-11/",
    "research_allowed_domains": ["nasa.gov"],
}


def verify_context(env: dict[str, str]) -> None:
    expected = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1",
                "GITHUB_RUN_ATTEMPT": "1", "RESEARCH_DIAGNOSTIC_APPROVED": "true",
                "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/main"}
    if any(env.get(key) != value for key, value in expected.items()):
        raise rf.ResearchBlocked("Not the sole approved diagnostic run and attempt.")
    sha = env.get("GITHUB_SHA", "")
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise rf.ResearchBlocked("Diagnostic source identity is unavailable.")
    run_id = env.get("GITHUB_RUN_ID", "")
    if not run_id.isascii() or not run_id.isdigit():
        raise rf.ResearchBlocked("Diagnostic run identity is unavailable.")


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
        if len(source_commit) != 40 or any(c not in "0123456789abcdef" for c in source_commit):
            raise ValueError()
        if git("status", "--porcelain", "--untracked-files=no"):
            raise ValueError()
    except Exception:
        raise rf.ResearchBlocked("Diagnostic requires a clean, separate fixed trigger-only commit.") from None
    return source_commit


def verify_history(env: dict[str, str], data: dict) -> None:
    runs = data.get("workflow_runs")
    if data.get("total_count") != 1 or not isinstance(runs, list) or len(runs) != 1:
        raise rf.ResearchBlocked("Diagnostic history is incomplete or already attempted.")
    expected = {"head_sha": env["GITHUB_SHA"], "head_branch": "main", "event": "push",
                "path": f".github/workflows/{WORKFLOW}", "run_number": 1, "run_attempt": 1,
                "status": "in_progress", "conclusion": None}
    run = runs[0]
    if (not isinstance(run, dict) or str(run.get("id")) != env["GITHUB_RUN_ID"]
            or any(run.get(key) != value for key, value in expected.items())):
        raise rf.ResearchBlocked("Cannot verify the sole diagnostic run for this commit.")


def check_gate(env: dict[str, str]) -> str:
    verify_context(env)
    source_commit = verify_trigger_commit(env)
    verify_history(env, read_actions_json(env, f"workflows/{WORKFLOW}/runs?per_page=100"))
    return source_commit


def new_record() -> dict:
    return {"schema_version": 1, "test": "resolved_apollo_research_diagnostic_only",
            "forecast_skill": "not_evaluated", "metaculus_submissions": 0,
            "model": rf.MODEL, "allowed_domains": ["nasa.gov"],
            "operational_status": "not_started", "request_attempts": 0,
            "max_requests": 1, "max_search_calls": rf.MAX_SEARCH_CALLS,
            "sdk_retries": 0, "reserved_usd": "0",
            "reservation_is_billing_cap": False, "estimated_cost_usd": None,
            "usage_complete": False, "request_sha256": None,
            "response_id": None, "response_usage_metadata": {
                "input_tokens": None, "output_tokens": None},
            "usage_observation": None, "failure_diagnostic": None,
            "citation_count": None, "sources_validated": False,
            "started_at_utc": None, "completed_at_utc": None}


def save_checkpoint(record: dict) -> None:
    temporary = ARTIFACT_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ARTIFACT_PATH)


async def run_probe(client, record: dict, *, checkpoint: Callable[[dict], None],
                    clock=pf.utc_now) -> dict:
    """Use the actual prospective request builder, but only a resolved fixture.

    No prospective manifest or forecast runner is loaded or executed. Response
    text, source URLs, queries, exception text/bodies/headers are never persisted.
    The caller supplies the durable sole-run claim before entering this method.
    """
    now = clock()
    if now >= rf.PRICING_REVIEW_DEADLINE or record["request_attempts"] != 0:
        raise rf.ResearchBlocked("Diagnostic is expired or already attempted.")
    payload = pf.research_request(QUESTION, pf.question_input(QUESTION, now.isoformat()))
    guarded = client.with_options(base_url=rf.API_BASE, max_retries=0, timeout=90)
    record.update(operational_status="attempted", request_attempts=1,
                  reserved_usd=RESERVATION_USD, request_sha256=pf.sha256(payload),
                  started_at_utc=now.isoformat())
    checkpoint(record)  # Fail before sending if durable recording fails.
    phase, received = "sdk_request", False
    try:
        response = await asyncio.wait_for(guarded.responses.create(**payload), timeout=90)
        received = True
        phase = "response_metadata"
        pf._capture_response_metadata(response, record)
        observation = rf._usage_observation(response, "research")
        record["usage_observation"] = observation
        record["usage_complete"] = observation is not None
        record["estimated_cost_usd"] = observation["estimated_cost_usd"] if observation is not None else None
        phase = "response_validate"
        parsed = rf._read_response(response, research=True, accessed_at=clock().isoformat())
        phase = "source_validate"
        pf.validate_research_sources(parsed, QUESTION["research_allowed_domains"])
        record.update(operational_status="completed", citation_count=len(parsed["citations"]),
                      sources_validated=True)
    except Exception as exc:
        record["operational_status"] = "failed"
        record["failure_diagnostic"] = pf._failure_diagnostic(
            exc, phase=phase, response_received=received)
    finally:
        record["completed_at_utc"] = clock().isoformat()
        checkpoint(record)
    return record


async def run_once(record: dict, *, env: dict, source_commit: str) -> dict:
    # This exclusive local claim complements the permanent sole Actions run.
    # Any failure consumes the claim; no retry, reset, or fallback is provided.
    with CLAIM_PATH.open("x", encoding="utf-8") as claim:
        json.dump({"run_id": env["GITHUB_RUN_ID"], "source_commit": source_commit,
                   "reserved_usd": RESERVATION_USD}, claim)
        claim.flush()
        os.fsync(claim.fileno())
    record.update(source_commit=source_commit, trigger_commit=env["GITHUB_SHA"],
                  github_run_id=env["GITHUB_RUN_ID"], reserved_usd=RESERVATION_USD)
    save_checkpoint(record)
    from openai import AsyncOpenAI
    async with AsyncOpenAI(base_url=rf.API_BASE, max_retries=0, timeout=90) as client:
        return await run_probe(client, record, checkpoint=save_checkpoint)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-gate", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.check_gate and not args.execute:
        print("Offline only. See RESEARCH_DIAGNOSTIC.md.")
        return
    logging.disable(logging.CRITICAL)
    env = dict(os.environ)
    try:
        source_commit = check_gate(env)
    except Exception:
        raise SystemExit("Diagnostic gate blocked; no paid request sent.") from None
    if args.check_gate:
        print("One-shot diagnostic gate verified.")
        return
    record = new_record()
    try:
        asyncio.run(run_once(record, env=env, source_commit=source_commit))
    except Exception as exc:
        if "source_commit" not in record:
            raise SystemExit("Diagnostic claim failed. Preserve prior artifact; do not retry.") from None
        # Client setup, claim, or checkpoint error: preserve any prior request
        # evidence without claiming a provider response or a zero-cost failure.
        record["operational_status"] = "failed"
        record["runner_failure_diagnostic"] = pf._failure_diagnostic(
            exc, phase="runner", response_received=False)
        try:
            save_checkpoint(record)
        except Exception:
            raise SystemExit("Diagnostic stopped; artifact write failed. Do not retry.") from None
    print(json.dumps(record, sort_keys=True))
    if record["operational_status"] != "completed":
        raise SystemExit("Diagnostic did not complete. Retain safe diagnostics; do not retry.")


if __name__ == "__main__":
    main()
