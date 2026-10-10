"""Resolved Apollo research and factual answer; no forecast or original-pilot rerun.

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
import research_compatibility_v1 as compatibility
from research_integration_check import FIXTURE
from safe_smoke_test import REPOSITORY, read_actions_json

WORKFLOW = "research_compatibility_20261010.yaml"
TRIGGER_PATH = ".github/research-compatibility-20261010.trigger"
TRIGGER_TEXT = "apollo-research-factual-answer-compatibility-v1\n"
ARTIFACT_PATH = Path("research-compatibility-result.json")
CLAIM_PATH = Path(".research-compatibility-20261010.claim")
# One corrected research request, then one tool-free factual answer. No retries.
RESERVATION_USD = "0.50"
AUTHORIZED_LIMIT_USD = "2.50"
MAX_REQUESTS = 2
PRIOR_BATCH_REQUESTS = 2
BATCH_REQUEST_LIMIT = 4
PRIOR_RUN_ID = 38072988473
PRIOR_KNOWN_COST_USD = "0.0172996"
PRIOR_TRIGGER_COMMIT = "a64611d099906deeeb05d7a73f4e97df88fb3784"
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
                "GITHUB_RUN_ATTEMPT": "1", "RESEARCH_COMPATIBILITY_APPROVED": "true",
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
    verify_prior_batch(read_actions_json(env, "workflows/research_filter_comparison_20261010.yaml/runs?per_page=100"))
    return source_commit


def verify_prior_batch(data: dict) -> None:
    runs = data.get("workflow_runs")
    expected = {"id": PRIOR_RUN_ID, "head_sha": PRIOR_TRIGGER_COMMIT,
                "head_branch": "main", "event": "push", "run_number": 1,
                "run_attempt": 1, "status": "completed", "conclusion": "success",
                "path": ".github/workflows/research_filter_comparison_20261010.yaml"}
    if (data.get("total_count") != 1 or not isinstance(runs, list) or len(runs) != 1
            or not isinstance(runs[0], dict) or any(runs[0].get(k) != v for k, v in expected.items())):
        raise rf.ResearchBlocked("Cannot verify the completed two-request first stage.")


def new_record() -> dict:
    return {"schema_version": 1, "test": "resolved_apollo_compatibility_factual_answer_only",
            "method": compatibility.METHOD_ID, "market_blind_research": False,
            "forecast_skill": "not_evaluated", "metaculus_submissions": 0,
            "model": rf.MODEL, "operational_status": "not_started",
            "request_attempts": 0, "max_requests": MAX_REQUESTS,
            "prior_batch_requests": PRIOR_BATCH_REQUESTS, "batch_request_limit": BATCH_REQUEST_LIMIT,
            "batch_request_attempts": PRIOR_BATCH_REQUESTS, "prior_batch_run_id": PRIOR_RUN_ID,
            "max_search_calls": rf.MAX_SEARCH_CALLS, "sdk_retries": 0,
            "reserved_usd": "0", "batch_reserved_usd": "0.50",
            "authorized_limit_usd": AUTHORIZED_LIMIT_USD, "reservation_is_billing_cap": False,
            "known_estimated_cost_usd": "0", "total_estimated_cost_usd": None,
            "known_batch_estimated_cost_usd": PRIOR_KNOWN_COST_USD,
            "total_batch_estimated_cost_usd": None, "usage_complete": False,
            "batch_usage_complete": False, "as_of_utc": None, "completed_at_utc": None,
            "requests": [], "source_provenance": None, "factual_answer": None,
            "known_fact_agrees": None}


def save_checkpoint(record: dict) -> None:
    temporary = ARTIFACT_PATH.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, ARTIFACT_PATH)


async def run_pipeline(client, record: dict, *, checkpoint: Callable[[dict], None],
                       clock=pf.utc_now) -> dict:
    now = clock()
    if now >= rf.PRICING_REVIEW_DEADLINE or record["request_attempts"] != 0 or record["requests"]:
        raise rf.ResearchBlocked("Compatibility check is expired or already started.")
    data = pf.question_input(QUESTION, now.isoformat())
    research_payload = compatibility.research_request(QUESTION, data)
    guarded = client.with_options(base_url=rf.API_BASE, max_retries=0, timeout=90)
    record.update(operational_status="in_progress", as_of_utc=now.isoformat(),
                  reserved_usd=RESERVATION_USD, batch_reserved_usd="1.00")
    checkpoint(record)

    async def request(stage: str, payload: dict, *, research: bool) -> dict:
        if (record["request_attempts"] >= MAX_REQUESTS or record["batch_request_attempts"] >= BATCH_REQUEST_LIMIT
                or clock() >= rf.PRICING_REVIEW_DEADLINE
                or Decimal(record["known_batch_estimated_cost_usd"]) + Decimal("0.25") > Decimal(AUTHORIZED_LIMIT_USD)):
            raise rf.ResearchBlocked("Request or known-cost allowance exhausted.")
        attempt = {"stage": stage, "request_sha256": pf.sha256(payload), "status": "attempted",
                   "started_at_utc": clock().isoformat(), "completed_at_utc": None,
                   "sdk_returned": False, "response_id": None, "response_usage_metadata": {
                       "input_tokens": None, "output_tokens": None}, "usage_observation": None,
                   "estimated_cost_usd": None, "failure_diagnostic": None}
        record["requests"].append(attempt)
        record["request_attempts"] += 1
        record["batch_request_attempts"] += 1
        record["usage_complete"] = False
        record["total_estimated_cost_usd"] = None
        checkpoint(record)
        phase = "sdk_request"
        try:
            response = await asyncio.wait_for(guarded.responses.create(**payload), timeout=90)
            attempt["sdk_returned"] = True
            phase = "response_metadata"
            pf._capture_response_metadata(response, attempt)
            observation = rf._usage_observation(response, stage)
            attempt["usage_observation"] = observation
            if observation is not None:
                cost = observation["estimated_cost_usd"]
                attempt["estimated_cost_usd"] = cost
                record["known_estimated_cost_usd"] = str(Decimal(record["known_estimated_cost_usd"]) + Decimal(cost))
                record["known_batch_estimated_cost_usd"] = str(Decimal(PRIOR_KNOWN_COST_USD)
                    + Decimal(record["known_estimated_cost_usd"]))
            phase = "response_validate"
            parsed = rf._read_response(response, research=research, accessed_at=clock().isoformat())
            if research:
                phase = "source_validate"
                pf.validate_research_sources(parsed, QUESTION["research_allowed_domains"])
                record["source_provenance"] = compatibility.source_provenance(parsed)
            attempt["status"] = "completed"
            return parsed
        except Exception as exc:
            attempt["status"] = "failed"
            attempt["failure_diagnostic"] = diagnostics.failure_diagnostic(
                exc, phase=phase, response_received=attempt["sdk_returned"])
            raise rf.ResearchBlocked("Compatibility request failed; no retry.") from None
        finally:
            attempt["completed_at_utc"] = clock().isoformat()
            record["usage_complete"] = all(a["usage_observation"] is not None for a in record["requests"])
            record["total_estimated_cost_usd"] = record["known_estimated_cost_usd"] if record["usage_complete"] else None
            checkpoint(record)

    phase = "research"
    try:
        research = await request("research", research_payload, research=True)
        phase = "answer_build"
        answer_payload = compatibility.answer_request(data, research)
        phase = "factual_answer"
        answer_response = await request("factual_answer", answer_payload, research=False)
        phase = "answer_parse"
        answer = compatibility.parse_answer(answer_response["text"], {c["url"] for c in research["citations"]})
        record["factual_answer"] = {"answer": answer["answer"], "source_urls": [
            u for u in answer["source_urls"] if compatibility.public_source_url(u)]}
        record["known_fact_agrees"] = answer["answer"] == "yes"
        record["operational_status"] = "completed"
    except Exception as exc:
        record["operational_status"] = "failed"
        record["pipeline_failure_diagnostic"] = diagnostics.failure_diagnostic(
            exc, phase=phase, response_received=False)
    finally:
        record["completed_at_utc"] = clock().isoformat()
        checkpoint(record)
    return record


async def run_once(record: dict, *, env: dict, source_commit: str) -> dict:
    with CLAIM_PATH.open("x", encoding="utf-8") as claim:
        json.dump({"run_id": env["GITHUB_RUN_ID"], "source_commit": source_commit,
                   "reserved_usd": RESERVATION_USD, "prior_batch_run_id": PRIOR_RUN_ID}, claim)
        claim.flush()
        os.fsync(claim.fileno())
    record.update(source_commit=source_commit, trigger_commit=env["GITHUB_SHA"],
                  github_run_id=env["GITHUB_RUN_ID"], reserved_usd=RESERVATION_USD, batch_reserved_usd="1.00")
    save_checkpoint(record)
    from openai import AsyncOpenAI
    async with AsyncOpenAI(base_url=rf.API_BASE, max_retries=0, timeout=90) as client:
        return await run_pipeline(client, record, checkpoint=save_checkpoint)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-gate", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.check_gate and not args.execute:
        print("Offline only. See RESEARCH_COMPATIBILITY.md.")
        return
    logging.disable(logging.CRITICAL)
    env = dict(os.environ)
    try:
        source_commit = check_gate(env)
    except Exception:
        raise SystemExit("Compatibility gate blocked; no paid request sent.") from None
    if args.check_gate:
        print("One-shot compatibility gate and prior batch verified.")
        return
    record = new_record()
    try:
        asyncio.run(run_once(record, env=env, source_commit=source_commit))
    except Exception as exc:
        if "source_commit" not in record:
            raise SystemExit("Compatibility claim failed. Preserve prior artifact; do not retry.") from None
        record["operational_status"] = "runner_failed"
        record["runner_failure_diagnostic"] = diagnostics.failure_diagnostic(
            exc, phase="runner", response_received=False)
        try:
            save_checkpoint(record)
        except Exception:
            raise SystemExit("Compatibility stopped; artifact write failed. Do not retry.") from None
    print(json.dumps(record, sort_keys=True))
    if record["operational_status"] != "completed" or record["known_fact_agrees"] is not True:
        raise SystemExit("Resolved-fact compatibility was not verified. No retry.")


if __name__ == "__main__":
    main()


