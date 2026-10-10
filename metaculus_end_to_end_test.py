"""One approved test-area forecast and private explanation, with readback.

No action on import or without --execute. Never retries a write or chooses a
second question after paid work. The previous synthetic campaign remains locked.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import http.client
import json
import logging
import math
import os
from typing import Any
from urllib.parse import urlencode

from check_metaculus_connection import HOST, check_connection
from safe_smoke_test import (API_BASE, END_TO_END_WORKFLOW, MAX_REQUEST_ATTEMPTS,
    MODEL, NoMetaculusAccess, RequestBudget, RESERVATION_PER_REQUEST_USD,
    TOTAL_AUTHORIZED_USD, WORKFLOW, check_paid_authorization, make_guarded_llm,
    read_actions_json, read_workflow_history, verify_campaign_context)

TEST_SLUG = "bot-testing-area"
PROJECT_PATH = f"/api/projects/tournaments/{TEST_SLUG}/"
LIST_PATH = f"/api/posts/?tournaments={TEST_SLUG}&statuses=open&forecast_type=binary&limit=100&offset=0"
WRITE_PATH = "/api/questions/bulk-forecast-comment/"
PRIOR_RUN_ID = 38017773835
PRIOR_COMMIT = "0742dd17b7d945bbafbe098deab0341cfebdfbe6"
PRIOR_RESERVED_USD = Decimal("1.00")
MAX_RESPONSE_BYTES = 1024 * 1024
# Both immutable source runs were reviewed as preflight-only, with zero API work.
# This is a one-time reconciliation, never a generic failed-run exemption.
PREFLIGHT_COMMIT = "199d840c3c0a3866b28b3a35f6c38f9ea4623441"
PREFLIGHT_RUNS = ((38018946468, 1, 114115438500), (38019014010, 2, 114115645940))
E2E_WORKFLOW_ID = 380215345
RECOVERY_RUN_NUMBER = 4
SELECTOR_RUN = (38019568451, 3, 114117334084)
SELECTOR_COMMIT = "31f3f2b5ab2c50f4d1ee8355c85faaf47fe6bdf5"
RECONCILED_RUNS = PREFLIGHT_RUNS + (SELECTOR_RUN,)
PREFLIGHT_STEPS = {
    "Set up job": "success",
    "Run actions/checkout@v4": "success",
    "Run actions/setup-python@v5": "success",
    "Run offline safety tests without secrets": "success",
    "Verify one-shot history and cumulative authorization": "failure",
    "Install Poetry": "skipped",
    "Install locked dependencies": "skipped",
    "Verify baseline formats with fake responses": "skipped",
    "Check bot identity, submit one test forecast, and verify readback": "skipped",
    "Post Run actions/setup-python@v5": "skipped",
    "Post Run actions/checkout@v4": "success",
    "Complete job": "success",
}
# Manually reviewed immutable run 3 log ended with the exact select_target error
# before RequestBudget construction, make_forecast, or post_once in SELECTOR_COMMIT.
# Job failure alone is NOT proof of no effects; this exception names that one
# already-reconciled log/run/job only. Any rerun, changed source or metadata blocks.
SELECTOR_STEPS = PREFLIGHT_STEPS | {
    "Verify one-shot history and cumulative authorization": "success",
    "Install Poetry": "success",
    "Install locked dependencies": "success",
    "Verify baseline formats with fake responses": "success",
    "Check bot identity, submit one test forecast, and verify readback": "failure",
}
TAXONOMY_TYPES = frozenset({"category", "topic", "news_category"})
# Closed vocabulary only. Never print an upstream message, payload, or user data.
SELECTION_REASONS = {
    "Test question identity does not match.": "post_identity",
    "Test question memberships are unavailable.": "memberships_missing",
    "Question is not owned by the designated test area.": "wrong_default_project",
    "Question memberships cannot be verified.": "memberships_malformed",
    "Question has a visible membership outside the test area.": "outside_scoring_membership",
    "Only a standalone binary test question is allowed.": "not_standalone_binary",
    "Question and post identifiers do not match.": "question_post_mismatch",
    "Question is not approved and unresolved.": "not_approved_unresolved",
    "Bot forecasting permission is not established for this question.": "forecast_permission",
    "Question timing cannot be verified.": "timing_missing_or_malformed",
    "Question is not open for forecasting.": "not_open",
    "Question is already closed.": "closed",
    "Question has already resolved.": "resolved",
    "Question resolution cannot be verified as undecided.": "resolution_not_null",
    "Question text or resolution criteria are unavailable.": "question_text_missing",
}


class TestBlocked(RuntimeError):
    """Fixed, safe-to-log messages only."""


def positive_id(value: Any) -> bool:
    return type(value) is int and value > 0


def check_prior_campaign(history: dict[str, Any]) -> None:
    runs = history.get("workflow_runs")
    if history.get("total_count") != 1 or not isinstance(runs, list) or len(runs) != 1:
        raise TestBlocked("Prior cumulative test history cannot be verified.")
    run = runs[0]
    expected = {"id": PRIOR_RUN_ID, "head_sha": PRIOR_COMMIT, "run_number": 1,
                "run_attempt": 1, "status": "completed", "conclusion": "success",
                "path": f".github/workflows/{WORKFLOW}"}
    if any(run.get(k) != v for k, v in expected.items()):
        raise TestBlocked("Prior test history changed; cumulative spending needs review.")
    if PRIOR_RESERVED_USD + MAX_REQUEST_ATTEMPTS * RESERVATION_PER_REQUEST_USD > TOTAL_AUTHORIZED_USD:
        raise TestBlocked("Cumulative test allowance would exceed the authorization.")


def verify_recovery_history(env: dict[str, str], history: dict[str, Any]) -> None:
    runs = history.get("workflow_runs")
    if history.get("total_count") != 4 or not isinstance(runs, list) or len(runs) != 4:
        raise TestBlocked("Recovery needs exactly the three reviewed no-spend runs and this run.")
    if any(not isinstance(run, dict) or not positive_id(run.get("id")) for run in runs):
        raise TestBlocked("Recovery run identities are unavailable.")
    by_id = {run["id"]: run for run in runs}
    current_id = env.get("GITHUB_RUN_ID", "")
    source_sha = env.get("GITHUB_SHA", "")
    if not current_id.isdigit() or len(source_sha) != 40 or any(c not in "0123456789abcdef" for c in source_sha):
        raise TestBlocked("Current recovery run identity cannot be established.")
    expected_ids = {run_id for run_id, _, _ in RECONCILED_RUNS} | {int(current_id)}
    if len(expected_ids) != 4 or set(by_id) != expected_ids:
        raise TestBlocked("Recovery history contains missing, duplicate, or unreviewed runs.")
    common = {"path": f".github/workflows/{END_TO_END_WORKFLOW}",
              "event": "workflow_dispatch", "head_branch": "main",
              "workflow_id": E2E_WORKFLOW_ID, "run_attempt": 1}
    for run_id, number, _ in RECONCILED_RUNS:
        source = SELECTOR_COMMIT if run_id == SELECTOR_RUN[0] else PREFLIGHT_COMMIT
        expected = common | {"head_sha": source, "run_number": number,
                             "status": "completed", "conclusion": "failure"}
        if any(by_id[run_id].get(k) != v for k, v in expected.items()):
            raise TestBlocked("A reviewed no-effect preflight changed; stop for reconciliation.")
    current = common | {"head_sha": source_sha, "run_number": RECOVERY_RUN_NUMBER,
                        "status": "in_progress", "conclusion": None}
    if any(by_id[int(current_id)].get(k) != v for k, v in current.items()):
        raise TestBlocked("This is not the single reviewed recovery dispatch.")


def verify_preflight_jobs(data: dict[str, Any], run_id: int, job_id: int) -> None:
    if (run_id, job_id) not in {(r, j) for r, _, j in RECONCILED_RUNS}:
        raise TestBlocked("No generic failed-job exemption is permitted.")
    source = SELECTOR_COMMIT if run_id == SELECTOR_RUN[0] else PREFLIGHT_COMMIT
    outcomes = SELECTOR_STEPS if run_id == SELECTOR_RUN[0] else PREFLIGHT_STEPS
    jobs = data.get("jobs")
    if data.get("total_count") != 1 or not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
        raise TestBlocked("No-effect preflight job evidence is incomplete.")
    job = jobs[0]
    expected = {"id": job_id, "run_id": run_id, "run_attempt": 1,
                "head_sha": source, "name": "test_area_check",
                "status": "completed", "conclusion": "failure"}
    if any(job.get(k) != v for k, v in expected.items()):
        raise TestBlocked("No-effect preflight job identity changed.")
    steps = job.get("steps")
    if not isinstance(steps, list) or len(steps) != len(outcomes):
        raise TestBlocked("No-effect preflight steps are incomplete.")
    if any(not isinstance(step, dict) or not isinstance(step.get("name"), str) for step in steps):
        raise TestBlocked("No-effect preflight steps are malformed.")
    by_name = {step["name"]: step for step in steps}
    if set(by_name) != set(outcomes) or any(
            by_name[name].get("status") != "completed" or by_name[name].get("conclusion") != outcome
            for name, outcome in outcomes.items()):
        raise TestBlocked("A preflight may have reached effect-bearing steps; recovery is blocked.")


def check_end_to_end_gate(env: dict[str, str]) -> None:
    if env.get("METACULUS_TEST_SUBMISSION_APPROVED") != "true":
        raise TestBlocked("One test-area forecast and private explanation are not approved.")
    check_paid_authorization(env)
    verify_campaign_context(env, END_TO_END_WORKFLOW, RECOVERY_RUN_NUMBER)
    verify_recovery_history(env, read_workflow_history(env, END_TO_END_WORKFLOW))
    for run_id, _, job_id in RECONCILED_RUNS:
        jobs = read_actions_json(env, f"runs/{run_id}/attempts/1/jobs?per_page=100")
        verify_preflight_jobs(jobs, run_id, job_id)
    check_prior_campaign(read_workflow_history(env, WORKFLOW))


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TestBlocked("Question timing cannot be verified.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except Exception:
        raise TestBlocked("Question timing cannot be verified.") from None


def validate_project(project: Any) -> int:
    if not isinstance(project, dict) or not positive_id(project.get("id")):
        raise TestBlocked("Official test-area project identity is unavailable.")
    if project.get("slug") != TEST_SLUG or project.get("type") != "question_series":
        raise TestBlocked("The destination is not the official bot testing series.")
    return project["id"]


@dataclass(frozen=True)
class Target:
    post_id: int
    question_id: int
    project_id: int
    title: str
    description: str
    resolution_criteria: str
    fine_print: str
    fingerprint: str

    @property
    def url(self) -> str:
        return f"https://www.metaculus.com/questions/{self.post_id}/"


def validate_target(post: Any, post_id: int, project_id: int, now: datetime) -> Target:
    if not isinstance(post, dict) or post.get("id") != post_id or not positive_id(post_id):
        raise TestBlocked("Test question identity does not match.")
    projects = post.get("projects")
    if not isinstance(projects, dict):
        raise TestBlocked("Test question memberships are unavailable.")
    default = projects.get("default_project")
    if (not isinstance(default, dict) or default.get("id") != project_id
            or default.get("slug") != TEST_SLUG or default.get("type") != "question_series"):
        raise TestBlocked("Question is not owned by the designated test area.")
    for kind, value in projects.items():
        if kind not in {"default_project", "question_series", *TAXONOMY_TYPES}:
            raise TestBlocked("Question has a visible membership outside the test area.")
        entries = [value] if kind == "default_project" else value
        if not isinstance(entries, list):
            raise TestBlocked("Question memberships cannot be verified.")
        for entry in entries:
            if (not isinstance(entry, dict) or not positive_id(entry.get("id"))
                    or not isinstance(entry.get("slug"), str) or not entry["slug"]):
                raise TestBlocked("Question memberships cannot be verified.")
            expected_type = "question_series" if kind == "default_project" else kind
            if entry.get("type") != expected_type:
                raise TestBlocked("Question memberships cannot be verified.")
            if kind in TAXONOMY_TYPES:
                # Official serializers place taxonomy alongside actual projects.
                # These entries must still be well-formed and cannot claim scoring.
                if any(entry.get(k) is not None for k in ("score_type", "primary_leaderboard", "primary_leaderboard_id")):
                    raise TestBlocked("Question has a visible membership outside the test area.")
                continue
            if (kind not in {"default_project", "question_series"}
                    or entry["id"] != project_id or entry["slug"] != TEST_SLUG):
                raise TestBlocked("Question has a visible membership outside the test area.")
    if any(post.get(k) is not None for k in ("conditional", "group_of_questions", "notebook")):
        raise TestBlocked("Only a standalone binary test question is allowed.")
    question = post.get("question")
    if not isinstance(question, dict) or not positive_id(question.get("id")) or question.get("type") != "binary":
        raise TestBlocked("Only a standalone binary test question is allowed.")
    if question.get("post_id") != post_id:
        raise TestBlocked("Question and post identifiers do not match.")
    if post.get("curation_status") != "approved" or post.get("resolved") is not False:
        raise TestBlocked("Question is not approved and unresolved.")
    if post.get("user_permission") not in {"forecaster", "curator", "admin", "creator"}:
        raise TestBlocked("Bot forecasting permission is not established for this question.")
    for item in (post, question):
        if item.get("status") != "open" or parse_time(item.get("open_time")) > now:
            raise TestBlocked("Question is not open for forecasting.")
        if parse_time(item.get("scheduled_close_time")) <= now:
            raise TestBlocked("Question is already closed.")
        # API supplies scheduled_close_time as actual_close_time when still open.
        if item.get("actual_close_time") is not None and parse_time(item["actual_close_time"]) <= now:
            raise TestBlocked("Question is already closed.")
        if item.get("actual_resolve_time") is not None:
            raise TestBlocked("Question has already resolved.")
    if "resolution" not in question or question["resolution"] is not None:
        raise TestBlocked("Question resolution cannot be verified as undecided.")
    texts = {}
    for key in ("title", "description", "resolution_criteria", "fine_print"):
        value = question.get(key)
        if value is None and key in {"description", "fine_print"}:
            value = ""
        # Official Question.resolution_criteria is blank=True. An empty string
        # is schema-valid for a connectivity fixture; never invent missing rules.
        if not isinstance(value, str) or (key == "title" and not value.strip()):
            raise TestBlocked("Question text or resolution criteria are unavailable.")
        texts[key] = value
    stable = {"post_id": post_id, "question_id": question["id"], "project_id": project_id,
              "texts": texts, "open_time": question["open_time"],
              "scheduled_close_time": question["scheduled_close_time"],
              "scheduled_resolve_time": question.get("scheduled_resolve_time")}
    fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    return Target(post_id, question["id"], project_id, fingerprint=fingerprint, **texts)


class MetaculusTestClient:
    """Literal HTTPS host, GET reads, and a single fixed atomic write endpoint."""
    def __init__(self, token: str, connection_factory=None):
        if not token or any(c.isspace() for c in token):
            raise TestBlocked("Metaculus credential is missing or malformed.")
        self._token = token
        self._factory = connection_factory or http.client.HTTPSConnection
        self._write_attempted = False

    def _request(self, method: str, path: str, payload=None):
        connection = None
        try:
            connection = self._factory(HOST, timeout=20)
            headers = {"Authorization": f"Token {self._token}", "Accept": "application/json"}
            body = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                body = json.dumps(payload).encode()
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status not in {200, 201}:
                raise TestBlocked("Metaculus request did not return a success status.")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise TestBlocked("Metaculus response exceeded the size limit.")
            return response.status, json.loads(data)
        except TestBlocked:
            raise
        except Exception:
            raise TestBlocked("Metaculus request failed; private response details were suppressed.") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def get(self, path: str):
        if not path.startswith("/api/") or "#" in path or "\r" in path or "\n" in path:
            raise TestBlocked("Invalid read path.")
        status, data = self._request("GET", path)
        if status != 200:
            raise TestBlocked("Metaculus read returned an unexpected status.")
        return data

    def post_once(self, payload: dict[str, Any]) -> bool:
        if self._write_attempted:
            raise TestBlocked("A submission was already attempted; never retry it.")
        self._write_attempted = True  # Before any network operation, even a timeout.
        try:
            status, data = self._request("POST", WRITE_PATH, payload)
            return status == 201 and data == {}
        except TestBlocked:
            return False  # Ambiguous; reconciliation is GET-only.


def forecast_history(client: MetaculusTestClient, question_id: int) -> list[dict[str, Any]]:
    data = client.get(f"/api/questions/bulk-forecast-read/?question_ids={question_id}")
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get("question_id") != question_id:
        raise TestBlocked("Own forecast history is unavailable; absence is not proof of no forecast.")
    history = rows[0].get("forecasts")
    if not isinstance(history, list):
        raise TestBlocked("Own forecast history is malformed.")
    return history


def select_target(client: MetaculusTestClient) -> Target:
    project_id = validate_project(client.get(PROJECT_PATH))
    listing = client.get(LIST_PATH)
    rows = listing.get("results") if isinstance(listing, dict) else None
    if not isinstance(rows, list):
        raise TestBlocked("Test-area question list is unavailable.")
    if len(rows) > 100:
        raise TestBlocked("Test-area listing exceeded the reviewed candidate limit.")
    ids = sorted({row["id"] for row in rows if isinstance(row, dict) and positive_id(row.get("id"))})
    diagnostics = {"listed_posts": len(rows), "candidate_ids": len(ids), "rejection_counts": {}, "selected": False}
    def record(code: str) -> None:
        counts = diagnostics["rejection_counts"]
        counts[code] = counts.get(code, 0) + 1
    def emit() -> None:
        print(json.dumps({"selection_diagnostics": diagnostics}, sort_keys=True), flush=True)
    for post_id in ids:
        detail = client.get(f"/api/posts/{post_id}/")
        try:
            target = validate_target(detail, post_id, project_id, datetime.now(timezone.utc))
        except TestBlocked as error:
            record(SELECTION_REASONS.get(str(error), "unclassified_validation_failure"))
            continue
        if forecast_history(client, target.question_id) == []:
            diagnostics["selected"] = True
            emit()  # Always before any budget reservation, paid call, or write.
            return target
        record("previously_forecasted")
    emit()
    raise TestBlocked("No eligible, previously unforecasted binary test-area question was found.")


def make_payload(target: Target, probability: float, reasoning: str, run_id: str) -> dict[str, Any]:
    if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0.001 <= probability <= 0.999:
        raise TestBlocked("Forecast probability is invalid.")
    if not isinstance(reasoning, str) or not reasoning.strip() or len(reasoning) > 20_000 or not run_id.isdigit():
        raise TestBlocked("Forecast explanation or run marker is invalid.")
    # The private explanation must not generate notifications through @mentions.
    text = (f"Setup test run {run_id}\n\nIntegration test only; not calibrated research.\n\n"
            f"{reasoning.strip().replace('@', '(at)')}")
    return {"forecasts": [{"question": target.question_id, "probability_yes": probability}],
            "comments": [{"on_post": target.post_id, "text": text,
                          "is_private": True, "included_forecast": True}]}


def verify_readback(client, target, payload, bot_id, started_at: float) -> tuple[bool, bool]:
    probability = payload["forecasts"][0]["probability_yes"]
    expected_text = payload["comments"][0]["text"]
    forecast_ok = comment_ok = False
    now = datetime.now(timezone.utc).timestamp()
    try:
        history = forecast_history(client, target.question_id)
        if len(history) == 1:
            row = history[0]
            values = row.get("forecast_values")
            forecast_ok = (row.get("author_id") == bot_id and row.get("question_id") == target.question_id
                and row.get("end_time") is None and isinstance(row.get("start_time"), (int, float))
                and started_at - 5 <= row["start_time"] <= now + 5
                and isinstance(values, list) and len(values) == 2
                and all(math.isclose(float(a), b, abs_tol=1e-9) for a, b in zip(values, [1 - probability, probability])))
    except Exception:
        pass
    try:
        query = urlencode({"post": target.post_id, "author": bot_id, "is_private": "true",
                           "sort": "-created_at", "limit": 100, "offset": 0})
        data = client.get(f"/api/comments/?{query}")
        rows = data.get("results") if isinstance(data, dict) else None
        if isinstance(rows, list):
            matches = []
            for row in rows:
                included = row.get("included_forecast")
                if (row.get("author", {}).get("id") == bot_id and row.get("on_post") == target.post_id
                    and row.get("is_private") is True and row.get("text") == expected_text
                    and row.get("is_soft_deleted") is False and isinstance(included, dict)
                    and included.get("question_type") == "binary"
                    and math.isclose(float(included.get("probability_yes")), probability, abs_tol=1e-9)
                    and started_at - 5 <= parse_time(row.get("created_at")).timestamp() <= now + 5):
                    matches.append(row)
            comment_ok = len(matches) == 1
    except Exception:
        pass
    return forecast_ok, comment_ok


async def make_forecast(target: Target, budget: RequestBudget):
    logging.disable(logging.CRITICAL)
    from openai import AsyncOpenAI
    from forecasting_tools import BinaryQuestion, GeneralLlm
    from main import FallTemplateBot2026
    async with AsyncOpenAI(base_url=API_BASE, max_retries=0, timeout=45) as client:
        llm = make_guarded_llm(GeneralLlm, client, budget)
        bot = FallTemplateBot2026(research_reports_per_question=1, predictions_per_research_report=1,
            enable_summarize_research=False, publish_reports_to_metaculus=False,
            metaculus_client=NoMetaculusAccess(),
            llms={"default": llm, "parser": llm, "summarizer": llm, "researcher": "no_research"})
        bot._structure_output_validation_samples = 1
        question = BinaryQuestion(question_text=target.title, background_info=target.description,
            resolution_criteria=target.resolution_criteria, fine_print=target.fine_print)
        return await bot._run_forecast_on_binary(question,
            "No external research. This is only a connectivity test in the official bot testing area. "
            "If the supplied resolution criteria are blank, state that they are unspecified; do not invent criteria. "
            "This output is an integration test, not calibrated research.")


def run_test(env: dict[str, str]) -> dict[str, Any]:
    check_end_to_end_gate(env)
    token = env.get("METACULUS_TOKEN", "")
    bot_id = check_connection(token)  # Internal only; never include in output.
    client = MetaculusTestClient(token)
    target = select_target(client)
    print(f"Selected official testing-area question: {target.url}", flush=True)
    budget = RequestBudget()
    prediction = asyncio.run(make_forecast(target, budget))
    payload = make_payload(target, prediction.prediction_value, prediction.reasoning, env["GITHUB_RUN_ID"])
    # Recheck exact destination and question after paid work, before the sole write.
    if validate_project(client.get(PROJECT_PATH)) != target.project_id:
        raise TestBlocked("Test project changed; nothing submitted.")
    fresh = validate_target(client.get(f"/api/posts/{target.post_id}/"), target.post_id,
                            target.project_id, datetime.now(timezone.utc))
    if fresh.fingerprint != target.fingerprint or forecast_history(client, target.question_id) != []:
        raise TestBlocked("Question changed or already has a bot forecast; nothing submitted.")
    started = datetime.now(timezone.utc).timestamp()
    print("Attempting the sole atomic test-area submission. Never rerun or resubmit after this point.", flush=True)
    accepted = client.post_once(payload)
    forecast_ok, comment_ok = verify_readback(client, target, payload, bot_id, started)
    result = {"question_url": target.url, "submission_response_confirmed": accepted,
              "forecast_verified": forecast_ok, "private_reasoning_verified": comment_ok,
              "request_attempts": budget.attempts, "new_reserved_usd": str(budget.attempts * RESERVATION_PER_REQUEST_USD),
              "cumulative_reserved_usd": str(PRIOR_RESERVED_USD + budget.attempts * RESERVATION_PER_REQUEST_USD),
              "observed_token_cost_usd": str(budget.observed_token_cost), "automatic_retry_allowed": False}
    if not (forecast_ok and comment_ok):
        print(json.dumps(result, indent=2), flush=True)
        raise TestBlocked("Submission outcome is not fully verified. Read-only reconciliation is required; never resubmit or rerun.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--check-gate", action="store_true")
    args = parser.parse_args()
    if not args.execute and not args.check_gate:
        print("Offline only. No API calls or submissions performed.")
        return
    try:
        if args.check_gate:
            check_end_to_end_gate(dict(os.environ))
            print("One-shot and cumulative-budget prerequisites passed.")
            return
        print(json.dumps(run_test(dict(os.environ)), indent=2))
    except (TestBlocked, RuntimeError) as error:
        # Only fixed safe diagnostics from our guards reach this boundary.
        if type(error) in {TestBlocked}:
            raise SystemExit(str(error)) from None
        raise SystemExit("Test stopped. Do not rerun or submit again until its outcome and cumulative spending are reviewed.") from None
    except Exception:
        raise SystemExit("Test stopped unexpectedly. Do not rerun or resubmit; private error details were suppressed.") from None


if __name__ == "__main__":
    main()
