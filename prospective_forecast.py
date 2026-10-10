"""Frozen 3–5-question pilot. Importing this module never accesses a network.

Both arms use the same forecast request; only the supplied research differs.
No discovery, trading, label retrieval, retry, or submission is implemented.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import researched_forecast as rf

PILOT_ID = "public-short-horizon-20261010-v1"
MAX_REQUESTS = 15
MAX_SEARCHES = 10
PAIR_SECONDS = 600
MIN_LEAD_SECONDS = 6 * 60 * 60
RESERVATION_USD = Decimal("2.50")  # Conservative reservation, not a billing ceiling.
FORECAST_INSTRUCTIONS = rf.FORECAST_INSTRUCTIONS + """When no research is supplied, use only the question and your prior knowledge.
If there are no supplied sources, source_urls must be an empty list; never
invent citations. Do not use community forecasts or market probabilities.
"""
QUESTION_FIELDS = ("question_id", "title", "description", "resolution_criteria", "fine_print",
                   "url", "event_time_utc", "event_start_time_utc", "expected_outcome_time_utc",
                   "resolution_source_url", "research_allowed_domains", "public")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("A timezone-aware timestamp is required.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("A timezone-aware timestamp is required.") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required.")
    return result.astimezone(timezone.utc)


def public_url(value: Any) -> bool:
    if not rf._url(value):
        return False
    url = urlsplit(value)
    return url.scheme == "https" and not url.query and not url.fragment


def sanitize_question(raw: dict) -> dict:
    """Allowlist, never forward arbitrary market/API fields to either model.

    This strips structured market probabilities and metadata, not probability
    claims embedded in prose. Human review of the frozen text remains required.
    """
    if not isinstance(raw, dict):
        raise ValueError("Each question must be an object.")
    result = {key: raw[key] for key in QUESTION_FIELDS if key in raw}
    required = ("question_id", "title", "resolution_criteria", "event_time_utc",
                "resolution_source_url", "url")
    if any(not isinstance(result.get(k), str) or not result[k].strip() for k in required):
        raise ValueError("Required question identity, rules, source, or timing is missing.")
    if result.get("public") is not True:
        raise ValueError("Only explicitly public questions are permitted.")
    for key in ("description", "fine_print"):
        result.setdefault(key, "")
        if not isinstance(result[key], str):
            raise ValueError("Question text must be strings.")
    if any(not public_url(result[k]) for k in ("url", "resolution_source_url")):
        raise ValueError("Question and resolution source must be public HTTPS URLs without query data.")
    domains = result.get("research_allowed_domains")
    if (not isinstance(domains, list) or not 1 <= len(domains) <= 10
            or any(not isinstance(d, str) or not re.fullmatch(r"[a-z0-9]+(?:[-.][a-z0-9]+)*\.[a-z]{2,63}", d)
                   for d in domains) or len(set(domains)) != len(domains)):
        raise ValueError("Freeze one to ten explicit primary research domains.")
    if any(d == blocked or d.endswith("." + blocked) for d in domains
           for blocked in ("manifold.markets", "metaculus.com", "polymarket.com", "kalshi.com")):
        raise ValueError("Prediction-market research domains are not permitted.")
    start = parse_utc(result["event_time_utc"])
    result["event_time_utc"] = start.isoformat()
    if "event_start_time_utc" in result and parse_utc(result["event_start_time_utc"]) != start:
        raise ValueError("Event start timestamps disagree.")
    result["event_start_time_utc"] = start.isoformat()
    if "expected_outcome_time_utc" in result:
        end = parse_utc(result["expected_outcome_time_utc"])
        if end < start:
            raise ValueError("Expected outcome cannot precede event start.")
        result["expected_outcome_time_utc"] = end.isoformat()
    return result


def normalize_manifest(raw: dict, *, now: datetime) -> dict:
    if (not isinstance(raw, dict) or raw.get("schema_version") != 1
            or raw.get("pilot_id") != PILOT_ID):
        raise ValueError("Unrecognized pilot manifest.")
    frozen = parse_utc(raw.get("frozen_at_utc"))
    if frozen > now:
        raise ValueError("Manifest must be frozen before execution.")
    questions = raw.get("questions")
    if not isinstance(questions, list) or not 3 <= len(questions) <= 5:
        raise ValueError("Freeze exactly three to five explicit questions.")
    clean = [sanitize_question(q) for q in questions]
    ids = [q["question_id"] for q in clean]
    urls = [q["url"] for q in clean]
    if len(set(ids)) != len(ids) or len(set(urls)) != len(urls):
        raise ValueError("Duplicate question IDs or URLs are not allowed.")
    for question in clean:
        check_lead(question, now)
        # Fail the entire manifest before any paid call if text is too long.
        value = question_input(question, now.isoformat())
        rf._bounded_request(FORECAST_INSTRUCTIONS, value | {"research": {}}, research=False)
        research_request(question, value)
    return {"schema_version": 1, "pilot_id": PILOT_ID,
            "frozen_at_utc": frozen.isoformat(), "questions": clean}


def check_lead(question: dict, now: datetime) -> None:
    if parse_utc(question["event_time_utc"]) - now <= timedelta(seconds=MIN_LEAD_SECONDS):
        raise ValueError("Event start must remain more than six hours in the future.")


def question_input(question: dict, as_of: str) -> dict:
    # Keep market URL/ID only as record provenance, not model input. Both arms
    # get exactly the same standalone wording, primary source and start time.
    snapshot = {k: question[k] for k in ("title", "description", "resolution_criteria", "fine_print")}
    snapshot.update(event_time_utc=question["event_time_utc"],
                    resolution_source_url=question["resolution_source_url"])
    return {"as_of_utc": as_of, "question": snapshot}


def research_request(question: dict, data: dict) -> dict:
    payload = rf._bounded_request(rf.RESEARCH_INSTRUCTIONS, data, research=True)
    payload["tools"][0]["filters"] = {"allowed_domains": question["research_allowed_domains"]}
    if len(rf._json(payload).encode("utf-8")) > rf.MAX_REQUEST_BYTES:
        raise rf.ResearchBlocked("Question and research filter exceed reviewed input bound.")
    return payload


def validate_research_sources(research: dict, domains: list[str]) -> None:
    for source in research["citations"] + research["sources"]:
        hostname = (urlsplit(source["url"]).hostname or "").lower()
        if not any(hostname == d or hostname.endswith("." + d) for d in domains):
            raise rf.ResearchBlocked("Research returned a source outside the frozen primary domains.")


PROTOCOL = {
    "version": PILOT_ID, "model": rf.MODEL, "max_output_tokens": rf.MAX_OUTPUT_TOKENS,
    "max_request_bytes": rf.MAX_REQUEST_BYTES, "temperature": 0.2,
    "max_requests": MAX_REQUESTS, "max_searches": MAX_SEARCHES,
    "max_searches_per_research": rf.MAX_SEARCH_CALLS, "max_pair_seconds": PAIR_SECONDS,
    "minimum_start_and_completion_lead_seconds_exclusive": MIN_LEAD_SECONDS,
    "arm_order": ["baseline", "research", "researched_forecast"],
    "sdk_retries": 0, "stateless": True,
    "research_domains": "Per-question frozen primary-domain allowlist; validate all returned sources", "failed_arm_probability": 0.5,
    "failed_questions_replaced": False, "forecast_prompt": FORECAST_INSTRUCTIONS,
    "research_prompt": rf.RESEARCH_INSTRUCTIONS, "forecast_schema": rf.FORECAST_SCHEMA,
    "scoring": "Brier and natural log loss; researched minus baseline; no probability clipping",
    "successful_pairs": "Both arms completed and pair completed within 600 seconds",
    "operational": "All resolved retained questions, failed arms at p=0.5",
    "reservation_usd": str(RESERVATION_USD), "reservation_is_billing_cap": False,
    "price_input_usd_per_million": str(rf.INPUT_USD_PER_MILLION),
    "price_output_usd_per_million": str(rf.OUTPUT_USD_PER_MILLION),
    "search_usd": str(rf.SEARCH_USD), "search_input_tokens": rf.SEARCH_INPUT_TOKENS,
    "pricing_review_deadline": rf.PRICING_REVIEW_DEADLINE.isoformat(),
}


def usage_complete(result: dict) -> bool:
    return all(request["usage_observation"] is not None or request["status"] == "not_sent_deadline"
               for record in result["records"] for arm in ("baseline", "researched")
               for request in record[arm]["requests"])


def new_arm() -> dict:
    return {"status": "failed", "failure_code": "not_attempted", "probability_yes": None,
            "operational_probability_yes": 0.5, "started_at_utc": None,
            "completed_at_utc": None, "requests": [], "forecast": None}


# Public diagnostics are a closed vocabulary. Never persist exception text,
# bodies, requests, headers, tracebacks, or arbitrary provider error codes.
_LOCAL_EXCEPTION_CLASSES = {
    rf.ResearchBlocked: "ResearchBlocked", NameError: "NameError",
    TypeError: "TypeError", AttributeError: "AttributeError",
    ValueError: "ValueError", TimeoutError: "TimeoutError",
}
_OPENAI_EXCEPTION_CLASSES = frozenset({
    "APIError", "APIStatusError", "APIConnectionError", "APITimeoutError",
    "APIResponseValidationError", "BadRequestError", "AuthenticationError",
    "PermissionDeniedError", "NotFoundError", "ConflictError",
    "UnprocessableEntityError", "RateLimitError", "InternalServerError",
})
_PROVIDER_CODES = frozenset({
    "invalid_api_key", "insufficient_quota", "rate_limit_exceeded",
    "model_not_found", "context_length_exceeded", "unsupported_parameter",
    "unsupported_value", "invalid_value", "server_error",
})


def _exception_attribute(exc: Exception, name: str) -> Any:
    """A diagnostic getter must never replace the original failure."""
    try:
        return getattr(exc, name, None)
    except Exception:
        return None


def _failure_diagnostic(exc: Exception, *, phase: str, response_received: bool) -> dict:
    cls = type(exc)
    exception_class = _LOCAL_EXCEPTION_CLASSES.get(cls)
    if (exception_class is None and cls.__module__ in {"openai", "openai._exceptions"}
            and cls.__name__ in _OPENAI_EXCEPTION_CLASSES):
        exception_class = cls.__name__
    status = _exception_attribute(exc, "status_code")
    code = _exception_attribute(exc, "code")
    return {"phase": phase, "response_received": response_received,
            "exception_class": exception_class,
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "provider_code": code if type(code) is str and code in _PROVIDER_CODES else None}


def _capture_response_metadata(response: Any, attempt: dict) -> None:
    """Retain bounded identity/token metadata before strict response parsing.

    These counts are diagnostic only. They do not establish accepted usage,
    search counts, cost, or billing completeness.
    """
    data = response.model_dump(mode="json")
    if not isinstance(data, dict):
        return
    response_id = data.get("id")
    if (type(response_id) is str
            and re.fullmatch(r"resp_[A-Za-z0-9_-]{1,192}", response_id)):
        attempt["response_id"] = response_id
    usage = data.get("usage")
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                attempt["response_usage_metadata"][key] = value


async def run_pilot(raw_manifest: dict, client: Any, *,
                    reserve: Callable[[Decimal], None],
                    checkpoint: Callable[[dict], None],
                    clock: Callable[[], datetime] = utc_now,
                    monotonic: Callable[[], float] = time.monotonic) -> dict:
    """Caller supplies durable one-shot gate/reservation and public-safe storage.

    Checkpoint runs before every outbound attempt and after every completion.
    Initial records retain the entire frozen denominator even if interrupted.
    """
    now = clock()
    if now >= rf.PRICING_REVIEW_DEADLINE:
        raise rf.ResearchBlocked("Recheck model and pricing before a new pilot.")
    manifest = normalize_manifest(raw_manifest, now=now)
    records = [{"question_id": q["question_id"], "question": q, "question_sha256": sha256(q),
                "baseline": new_arm(), "researched": new_arm(), "pair_valid": False,
                "pair_started_at_utc": None, "pair_completed_at_utc": None,
                "pair_elapsed_seconds": None, "timing_status": "not_attempted"}
               for q in manifest["questions"]]
    result = {"schema_version": 1, "pilot_id": PILOT_ID, "model": rf.MODEL,
              "protocol": PROTOCOL, "protocol_sha256": sha256(PROTOCOL),
              "forecast_prompt_sha256": sha256(FORECAST_INSTRUCTIONS),
              "research_prompt_sha256": sha256(rf.RESEARCH_INSTRUCTIONS),
              "manifest": manifest, "manifest_sha256": sha256(manifest),
              "started_at_utc": now.isoformat(), "completed_at_utc": None,
              "run_status": "in_progress", "request_attempts": 0,
              "search_allowance_consumed": 0, "observed_search_calls": 0,
              "reserved_usd": str(RESERVATION_USD), "known_estimated_cost_usd": "0",
              "usage_complete": True, "forecast_skill": "not_evaluated",
              "records": records}
    guarded_client = client.with_options(base_url=rf.API_BASE, max_retries=0, timeout=90)
    reserve(RESERVATION_USD)
    checkpoint(result)

    for record in records:
        q = record["question"]
        pair_start = clock()
        pair_tick = monotonic()
        record["pair_started_at_utc"] = pair_start.isoformat()
        data = question_input(q, pair_start.isoformat())
        try:
            check_lead(q, pair_start)
        except ValueError:
            record["timing_status"] = "insufficient_lead"
            for arm in (record["baseline"], record["researched"]):
                arm["failure_code"] = "insufficient_lead"
            record["pair_completed_at_utc"] = clock().isoformat()
            record["pair_elapsed_seconds"] = max(0, monotonic() - pair_tick)
            checkpoint(result)
            continue

        async def request(stage: str, arm: dict, payload: dict, *, research: bool) -> dict:
            elapsed = max(monotonic() - pair_tick, (clock() - pair_start).total_seconds())
            if elapsed < 0 or elapsed >= PAIR_SECONDS or clock() >= parse_utc(q["event_time_utc"]) - timedelta(seconds=MIN_LEAD_SECONDS):
                raise rf.ResearchBlocked("Pair deadline reached; no further request.")
            if result["request_attempts"] >= MAX_REQUESTS:
                raise rf.ResearchBlocked("Pilot request allowance exhausted.")
            if research and result["search_allowance_consumed"] + rf.MAX_SEARCH_CALLS > MAX_SEARCHES:
                raise rf.ResearchBlocked("Pilot search allowance exhausted.")
            attempt = {"stage": stage, "request_sha256": sha256(payload),
                       "started_at_utc": clock().isoformat(), "completed_at_utc": None,
                       "response_id": None,
                       "response_usage_metadata": {"input_tokens": None, "output_tokens": None},
                       "failure_diagnostic": None,
                       "usage_observation": None, "status": "attempted"}
            arm["requests"].append(attempt)
            result["request_attempts"] += 1
            if research:
                result["search_allowance_consumed"] += rf.MAX_SEARCH_CALLS
            result["usage_complete"] = False
            checkpoint(result)  # Reservation survives failure/ambiguous response.
            # A slow durable write must not allow a request after the deadline.
            elapsed = max(monotonic() - pair_tick, (clock() - pair_start).total_seconds())
            if elapsed < 0 or elapsed >= PAIR_SECONDS or clock() >= parse_utc(q["event_time_utc"]) - timedelta(seconds=MIN_LEAD_SECONDS):
                attempt["status"] = "not_sent_deadline"
                attempt["completed_at_utc"] = clock().isoformat()
                result["usage_complete"] = usage_complete(result)
                checkpoint(result)
                raise rf.ResearchBlocked("Pair deadline reached before send; no further request.")
            response = None
            response_received = False
            phase = "sdk_request"
            try:
                response = await asyncio.wait_for(guarded_client.responses.create(**payload),
                                                  timeout=min(90, PAIR_SECONDS - elapsed,
                                                      (parse_utc(q["event_time_utc"]) - clock()).total_seconds() - MIN_LEAD_SECONDS))
                response_received = True
                phase = "response_metadata"
                _capture_response_metadata(response, attempt)
                observation = rf._usage_observation(response, stage)
                attempt["usage_observation"] = observation
                if observation is not None:
                    result["known_estimated_cost_usd"] = str(Decimal(result["known_estimated_cost_usd"])
                                                           + Decimal(observation["estimated_cost_usd"]))
                    result["observed_search_calls"] += observation["search_calls"]
                else:
                    result["usage_complete"] = False
                phase = "response_validate"
                parsed = rf._read_response(response, research=research, accessed_at=clock().isoformat())
                attempt["response"] = parsed
                if research:
                    phase = "source_validate"
                    validate_research_sources(parsed, q["research_allowed_domains"])
                attempt["status"] = "completed"
                return parsed
            except Exception as exc:
                attempt["status"] = "failed"
                attempt["failure_diagnostic"] = _failure_diagnostic(
                    exc, phase=phase, response_received=response_received)
                if attempt["usage_observation"] is None:
                    result["usage_complete"] = False
                raise rf.ResearchBlocked("Request or response validation failed; no retry.") from None
            finally:
                attempt["completed_at_utc"] = clock().isoformat()
                result["usage_complete"] = usage_complete(result)
                checkpoint(result)

        baseline = record["baseline"]
        baseline["started_at_utc"] = clock().isoformat()
        try:
            response = await request("baseline", baseline,
                rf._bounded_request(FORECAST_INSTRUCTIONS, data | {"research": {}}, research=False),
                research=False)
            forecast = rf._parse_forecast(response["text"], set(), allow_empty_sources=True)
            baseline.update(status="completed", failure_code=None, forecast=forecast,
                            probability_yes=forecast["probability_yes"],
                            operational_probability_yes=forecast["probability_yes"])
        except rf.ResearchBlocked:
            baseline["failure_code"] = "baseline_failed_no_retry"
        baseline["completed_at_utc"] = clock().isoformat()
        checkpoint(result)

        researched = record["researched"]
        researched["started_at_utc"] = clock().isoformat()
        try:
            evidence = await request("research", researched,
                research_request(q, data), research=True)
            response = await request("researched_forecast", researched,
                rf._bounded_request(FORECAST_INSTRUCTIONS, data | {"research": {
                    k: evidence[k] for k in ("text", "citations")}}, research=False), research=False)
            forecast = rf._parse_forecast(response["text"], {c["url"] for c in evidence["citations"]})
            researched.update(status="completed", failure_code=None, forecast=forecast,
                              probability_yes=forecast["probability_yes"],
                              operational_probability_yes=forecast["probability_yes"])
        except rf.ResearchBlocked:
            researched["failure_code"] = "researched_failed_no_retry"
        researched["completed_at_utc"] = clock().isoformat()
        finish = clock()
        elapsed = max(monotonic() - pair_tick, (finish - pair_start).total_seconds())
        record["pair_completed_at_utc"] = finish.isoformat()
        record["pair_elapsed_seconds"] = elapsed
        record["pair_valid"] = (0 <= elapsed <= PAIR_SECONDS and finish < parse_utc(q["event_time_utc"]) - timedelta(seconds=MIN_LEAD_SECONDS))
        record["timing_status"] = "valid" if record["pair_valid"] else "deadline_exceeded"
        checkpoint(result)
    result["run_status"] = "completed"
    result["completed_at_utc"] = clock().isoformat()
    checkpoint(result)
    return result
