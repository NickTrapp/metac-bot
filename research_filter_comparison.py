"""Two controlled Apollo research requests; no forecast or original-pilot rerun.

Default execution is offline. A new trigger-only commit and the sole first
Actions run are required before using the existing OpenAI Actions secret.
"""
from __future__ import annotations

import argparse
import copy
from decimal import Decimal
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
from typing import Callable

import prospective_forecast as pf
import researched_forecast as rf
import research_error_diagnostics as diagnostics
from research_integration_check import FIXTURE
from safe_smoke_test import REPOSITORY, read_actions_json

WORKFLOW = "research_filter_comparison_20261010.yaml"
TRIGGER_PATH = ".github/research-filter-comparison-20261010.trigger"
TRIGGER_TEXT = "apollo-filter-vs-unfiltered-comparison-v1\n"
ARTIFACT_PATH = Path("research-filter-comparison-result.json")
CLAIM_PATH = Path(".research-filter-comparison-20261010.claim")
# Two requests, each with at most two searches. No retries or extra variants.
RESERVATION_USD = "0.50"
AUTHORIZED_LIMIT_USD = "2.50"
MAX_REQUESTS = 2
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
                "GITHUB_RUN_ATTEMPT": "1", "RESEARCH_FILTER_COMPARISON_APPROVED": "true",
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


def build_payloads(as_of: str) -> list[tuple[str, dict]]:
    """Freeze the input once; only presence of the domain filter differs."""
    filtered = pf.research_request(QUESTION, pf.question_input(QUESTION, as_of))
    unfiltered = copy.deepcopy(filtered)
    del unfiltered["tools"][0]["filters"]
    return [("filtered", filtered), ("unfiltered", unfiltered)]


def new_attempt(variant: str, payload: dict) -> dict:
    return {"variant": variant, "request_sha256": pf.sha256(payload),
            "status": "not_attempted", "started_at_utc": None,
            "completed_at_utc": None, "sdk_returned": False,
            "response_id": None, "response_usage_metadata": {
                "input_tokens": None, "output_tokens": None},
            "failure_diagnostic": None, "usage_observation": None,
            "estimated_cost_usd": None, "citation_count": None,
            "sources_validated": False}


def new_record() -> dict:
    return {"schema_version": 1, "test": "resolved_apollo_filter_comparison_only",
            "forecast_skill": "not_evaluated", "metaculus_submissions": 0,
            "model": rf.MODEL, "operational_status": "not_started",
            "request_attempts": 0, "max_requests": MAX_REQUESTS,
            "max_search_calls_per_request": rf.MAX_SEARCH_CALLS, "sdk_retries": 0,
            "reserved_usd": "0", "authorized_limit_usd": AUTHORIZED_LIMIT_USD,
            "reservation_is_billing_cap": False, "known_estimated_cost_usd": "0",
            "total_estimated_cost_usd": None, "usage_complete": False,
            "as_of_utc": None, "completed_at_utc": None,
            "comparison_outcome": "inconclusive", "requests": []}


def save_checkpoint(record: dict) -> None:
    temporary = ARTIFACT_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ARTIFACT_PATH)


def comparison_outcome(attempts: list[dict]) -> str:
    if len(attempts) != 2 or any(a["status"] in {"not_attempted", "attempted"} for a in attempts):
        return "inconclusive"
    filtered, unfiltered = attempts
    first = filtered.get("failure_diagnostic") or {}
    second = unfiltered.get("failure_diagnostic") or {}
    if first.get("phase") == "sdk_request" and first.get("http_status") == 400 and unfiltered["sdk_returned"]:
        return "filtered_rejected_400_unfiltered_returned"
    if filtered["sdk_returned"] and unfiltered["sdk_returned"]:
        return "both_requests_returned"
    if (first.get("phase") == second.get("phase") == "sdk_request"
            and first.get("http_status") == second.get("http_status") == 400
            and first.get("provider_param") is not None
            and first.get("provider_param") == second.get("provider_param")):
        return "both_rejected_400_same_parameter"
    return "inconclusive"


async def run_comparison(client, record: dict, *, checkpoint: Callable[[dict], None],
                         clock=pf.utc_now) -> dict:
    now = clock()
    if now >= rf.PRICING_REVIEW_DEADLINE or record["request_attempts"] != 0 or record["requests"]:
        raise rf.ResearchBlocked("Comparison is expired or already started.")
    payloads = build_payloads(now.isoformat())
    guarded = client.with_options(base_url=rf.API_BASE, max_retries=0, timeout=90)
    record.update(operational_status="in_progress", reserved_usd=RESERVATION_USD,
                  as_of_utc=now.isoformat(), requests=[new_attempt(v, p) for v, p in payloads])
    checkpoint(record)
    for attempt, (_, payload) in zip(record["requests"], payloads):
        if (record["request_attempts"] >= MAX_REQUESTS or clock() >= rf.PRICING_REVIEW_DEADLINE
                or Decimal(record["known_estimated_cost_usd"]) + Decimal("0.25") > Decimal(AUTHORIZED_LIMIT_USD)):
            record["operational_status"] = "stopped_before_next_request"
            checkpoint(record)
            return record
        attempt.update(status="attempted", started_at_utc=clock().isoformat())
        record["request_attempts"] += 1
        checkpoint(record)  # No request if the durable pre-send write fails.
        phase = "sdk_request"
        try:
            response = await asyncio.wait_for(guarded.responses.create(**payload), timeout=90)
            attempt["sdk_returned"] = True
            phase = "response_metadata"
            pf._capture_response_metadata(response, attempt)
            observation = rf._usage_observation(response, "research")
            attempt["usage_observation"] = observation
            if observation is not None:
                attempt["estimated_cost_usd"] = observation["estimated_cost_usd"]
                record["known_estimated_cost_usd"] = str(Decimal(record["known_estimated_cost_usd"])
                    + Decimal(observation["estimated_cost_usd"]))
            phase = "response_validate"
            parsed = rf._read_response(response, research=True, accessed_at=clock().isoformat())
            # Evaluate source policy identically in both variants. Causal API
            # acceptance is based on sdk_returned, not local source acceptance.
            phase = "source_validate"
            pf.validate_research_sources(parsed, QUESTION["research_allowed_domains"])
            attempt.update(status="completed", citation_count=len(parsed["citations"]),
                           sources_validated=True)
        except Exception as exc:
            attempt["status"] = "failed"
            attempt["failure_diagnostic"] = diagnostics.failure_diagnostic(
                exc, phase=phase, response_received=attempt["sdk_returned"])
        finally:
            attempt["completed_at_utc"] = clock().isoformat()
            record["usage_complete"] = all(a["usage_observation"] is not None for a in record["requests"])
            record["total_estimated_cost_usd"] = record["known_estimated_cost_usd"] if record["usage_complete"] else None
            checkpoint(record)
    record.update(operational_status="completed_two_attempts", completed_at_utc=clock().isoformat(),
                  comparison_outcome=comparison_outcome(record["requests"]))
    checkpoint(record)
    return record


async def run_once(record: dict, *, env: dict, source_commit: str) -> dict:
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
        return await run_comparison(client, record, checkpoint=save_checkpoint)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-gate", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.check_gate and not args.execute:
        print("Offline only. See RESEARCH_FILTER_COMPARISON.md.")
        return
    logging.disable(logging.CRITICAL)
    env = dict(os.environ)
    try:
        source_commit = check_gate(env)
    except Exception:
        raise SystemExit("Comparison gate blocked; no paid request sent.") from None
    if args.check_gate:
        print("One-shot comparison gate verified.")
        return
    record = new_record()
    try:
        asyncio.run(run_once(record, env=env, source_commit=source_commit))
    except Exception as exc:
        if "source_commit" not in record:
            raise SystemExit("Comparison claim failed. Preserve prior artifact; do not retry.") from None
        record["operational_status"] = "runner_failed"
        record["runner_failure_diagnostic"] = diagnostics.failure_diagnostic(
            exc, phase="runner", response_received=False)
        try:
            save_checkpoint(record)
        except Exception:
            raise SystemExit("Comparison stopped; artifact write failed. Do not retry.") from None
    print(json.dumps(record, sort_keys=True))
    if record["operational_status"] != "completed_two_attempts":
        raise SystemExit("Comparison stopped before both attempts. No retry.")


if __name__ == "__main__":
    main()


