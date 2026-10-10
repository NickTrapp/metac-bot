"""One resolved public historical fixture, never a Metaculus submission.

Default execution is offline. The supported paid path is the sole first attempt
of a dedicated trigger-only push, after source publication and offline checks.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from researched_forecast import (API_BASE, BinaryQuestion, ResearchBlocked,
                                 RUN_RESERVATION_USD, forecast_binary)
from safe_smoke_test import REPOSITORY, read_actions_json

WORKFLOW = "research_integration_check.yaml"
TRIGGER_PATH = ".github/research-integration-20261010.trigger"
TRIGGER_TEXT = "apollo-11-resolved-fact-operational-check-v1\n"
ARTIFACT_PATH = Path("research-check-result.json")
FIXTURE = BinaryQuestion(
    title="Did Apollo 11's lunar module land on the Moon on July 20, 1969 (UTC)?",
    description="Resolved historical fact used solely for a public operational integration test.",
    resolution_criteria="Resolve Yes if official NASA mission history records Apollo 11's lunar "
                        "module landing on July 20, 1969 UTC; otherwise resolve No.",
    fine_print="Use NASA primary historical sources. This is not a prospective forecast or "
               "evidence of forecasting accuracy/calibration.")


def verify_context(env: dict[str, str]) -> None:
    expected = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_NUMBER": "1",
                "GITHUB_RUN_ATTEMPT": "1", "RESEARCH_CHECK_APPROVED": "true",
                "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/main"}
    if any(env.get(key) != value for key, value in expected.items()):
        raise ResearchBlocked("This is not the approved first research-check push and attempt.")
    sha = env.get("GITHUB_SHA", "")
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ResearchBlocked("Research-check source identity is unavailable.")


def verify_trigger_commit(env: dict[str, str]) -> None:
    def git(*args):
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL).strip()
    try:
        if git("rev-parse", "HEAD") != env["GITHUB_SHA"]:
            raise ValueError()
        # Code and workflow must be published first. The second commit can only
        # add this fixed trigger; it cannot smuggle in a runner or prompt edit.
        if git("diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD") != "A\t" + TRIGGER_PATH:
            raise ValueError()
        if Path(TRIGGER_PATH).read_text() != TRIGGER_TEXT:
            raise ValueError()
    except Exception:
        raise ResearchBlocked("The paid trigger must be a separate fixed trigger-only commit.") from None


def verify_history(env: dict[str, str], data: dict) -> None:
    runs = data.get("workflow_runs")
    if data.get("total_count") != 1 or not isinstance(runs, list) or len(runs) != 1:
        raise ResearchBlocked("Research-check history is incomplete or already attempted.")
    run = runs[0]
    expected = {"head_sha": env["GITHUB_SHA"], "head_branch": "main", "event": "push",
                "path": f".github/workflows/{WORKFLOW}", "run_number": 1, "run_attempt": 1,
                "status": "in_progress", "conclusion": None}
    if (not isinstance(run, dict) or str(run.get("id")) != env.get("GITHUB_RUN_ID")
            or any(run.get(key) != value for key, value in expected.items())):
        raise ResearchBlocked("Cannot verify the sole research-check run for this commit.")


def check_gate(env: dict[str, str]) -> None:
    verify_context(env)
    verify_trigger_commit(env)
    verify_history(env, read_actions_json(env, f"workflows/{WORKFLOW}/runs?per_page=100"))


def public_summary(record: dict) -> dict:
    research = record.get("research") or {}
    citations = research.get("citations", [])
    nasa = any((urlsplit(citation["url"]).hostname or "").lower() == "nasa.gov"
               or (urlsplit(citation["url"]).hostname or "").lower().endswith(".nasa.gov")
               for citation in citations)
    observations = record.get("usage_observations", [])
    cost = record.get("estimated_cost_usd")
    if cost is None:
        cost = str(sum((Decimal(item["estimated_cost_usd"]) for item in observations), Decimal("0")))
    probability = (record.get("forecast") or {}).get("probability_yes")
    known_label_agrees = type(probability) in (float, int) and 0.5 < probability <= 1
    return {"test": "resolved_historical_fact_operational_only",
            "operational_status": record.get("operational_status", "incomplete"),
            "request_attempts": record.get("request_attempts", 0),
            "reserved_usd": record.get("reserved_usd", "0"),
            "known_estimated_cost_usd": cost,
            "usage_complete": record.get("operational_status") == "completed"
                              or len(observations) == record.get("request_attempts", 0),
            "citation_count": len(citations), "nasa_citation_present": nasa,
            "known_label_agrees": known_label_agrees,
            "forecast_skill": "not_evaluated", "metaculus_submissions": 0}


async def run_check() -> dict:
    # No client/key access until the separately tested first-run gate passes.
    from openai import AsyncOpenAI
    reserved = False

    def reserve(amount: Decimal) -> None:
        nonlocal reserved
        if reserved or amount != RUN_RESERVATION_USD:
            raise ResearchBlocked("Research-check reservation already consumed or changed.")
        # The verified sole GitHub run is the durable one-shot claim. Failures
        # before/after this point never unlock another attempt or refund it.
        reserved = True
        print(json.dumps({"research_check_reserved_usd": str(amount)}), flush=True)

    async with AsyncOpenAI(base_url=API_BASE, max_retries=0, timeout=90) as client:
        return await forecast_binary(FIXTURE, client, reserve)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check-gate", action="store_true")
    modes.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.check_gate and not args.execute:
        print("Offline only. See RESEARCH_BASELINE.md.")
        return
    logging.disable(logging.CRITICAL)
    try:
        check_gate(dict(os.environ))
    except Exception:
        raise SystemExit("Research-check gate blocked; no paid request sent.") from None
    if args.check_gate:
        print("One-shot research-check gate verified.")
        return
    try:
        record = asyncio.run(run_check())
    except ResearchBlocked as exc:
        record = exc.partial_record or {"operational_status": "incomplete", "request_attempts": 0}
    except Exception:
        record = {"operational_status": "incomplete", "request_attempts": None}
    summary = public_summary(record)
    # Only the hard-coded public fixture and model-returned provenance; never
    # environment, headers, account data, credentials, or raw exception bodies.
    ARTIFACT_PATH.write_text(json.dumps({"summary": summary, "result": record}, indent=2))
    print(json.dumps(summary, sort_keys=True))
    if (summary["operational_status"] != "completed" or not summary["nasa_citation_present"]
            or not summary["known_label_agrees"]):
        raise SystemExit("Research integration was not verified. Review the fixture artifact; no retry.")


if __name__ == "__main__":
    main()
