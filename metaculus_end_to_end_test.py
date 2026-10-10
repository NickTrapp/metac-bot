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
    TOTAL_AUTHORIZED_USD, WORKFLOW, check_paid_gate, make_guarded_llm,
    read_workflow_history)

TEST_SLUG = "bot-testing-area"
PROJECT_PATH = f"/api/projects/tournaments/{TEST_SLUG}/"
LIST_PATH = f"/api/posts/?tournaments={TEST_SLUG}&statuses=open&forecast_type=binary&limit=100&offset=0"
WRITE_PATH = "/api/questions/bulk-forecast-comment/"
PRIOR_RUN_ID = 38017773835
PRIOR_COMMIT = "0742dd17b7d945bbafbe098deab0341cfebdfbe6"
PRIOR_RESERVED_USD = Decimal("1.00")
MAX_RESPONSE_BYTES = 1024 * 1024


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


def check_end_to_end_gate(env: dict[str, str]) -> None:
    if env.get("METACULUS_TEST_SUBMISSION_APPROVED") != "true":
        raise TestBlocked("One test-area forecast and private explanation are not approved.")
    check_paid_gate(env, END_TO_END_WORKFLOW)
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
    if not isinstance(default, dict) or default.get("id") != project_id or default.get("slug") != TEST_SLUG:
        raise TestBlocked("Question is not owned by the designated test area.")
    for value in projects.values():
        entries = [value] if isinstance(value, dict) else value
        if not isinstance(entries, list):
            raise TestBlocked("Question memberships cannot be verified.")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("id") != project_id or entry.get("slug") != TEST_SLUG:
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
        if not isinstance(value, str) or (key in {"title", "resolution_criteria"} and not value.strip()):
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
    ids = sorted({row["id"] for row in rows if isinstance(row, dict) and positive_id(row.get("id"))})
    for post_id in ids:
        detail = client.get(f"/api/posts/{post_id}/")
        try:
            target = validate_target(detail, post_id, project_id, datetime.now(timezone.utc))
        except TestBlocked:
            continue
        if forecast_history(client, target.question_id) == []:
            return target
    raise TestBlocked("No eligible, previously unforecasted binary test-area question was found.")


def make_payload(target: Target, probability: float, reasoning: str, run_id: str) -> dict[str, Any]:
    if isinstance(probability, bool) or not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0.001 <= probability <= 0.999:
        raise TestBlocked("Forecast probability is invalid.")
    if not isinstance(reasoning, str) or not reasoning.strip() or len(reasoning) > 20_000 or not run_id.isdigit():
        raise TestBlocked("Forecast explanation or run marker is invalid.")
    # The private explanation must not generate notifications through @mentions.
    text = f"Setup test run {run_id}\n\n{reasoning.strip().replace('@', '(at)')}"
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
            "No external research. This is only a connectivity test in the official bot testing area.")


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
