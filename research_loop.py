"""A small persistent forecasting loop. Default execution is an offline plan.

This is a new protocol; the October 10 pilot and its records are unchanged.
SQLite stores attempts before sending. Failed/ambiguous attempts are never
automatically repeated. The budget limits reservations, not provider invoices.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable
from urllib.parse import urlsplit

import prospective_forecast as pf
import researched_forecast as rf

STAGES = ("baseline", "research", "forecast")
PROTOCOL = {
    "version": "research-loop-v1", "research_model": "gpt-6-luna",
    "forecast_model": "gpt-6.1-sol", "forecast_reasoning": "high",
    "research_reasoning": "low", "max_output_tokens": 8192,
    "max_request_bytes": 32768, "max_searches": 2, "sdk_retries": 0,
    "reservation_per_request_usd": "0.25", "max_pair_seconds": 600,
    "minimum_lead_seconds": 21600, "prices_checked_utc": "2026-10-12",
    "pricing_review_deadline": "2026-10-19T00:00:00+00:00",
    "prices_per_million": {"gpt-6-luna": ["0.10", "0.50"],
                           "gpt-6.1-sol": ["2.00", "10.00"]},
    "search_call_usd": "0.01", "search_input_allowance_tokens": 8000,
    "baseline_prompt": pf.FORECAST_INSTRUCTIONS,
    "research_prompt": rf.RESEARCH_INSTRUCTIONS,
    "forecast_schema": rf.FORECAST_SCHEMA,
    "failure_policy": "retain; stop campaign; no retry or question replacement",
    "retrieval_policy": "primary domain filter plus citation validation",
    "market_blindness_verified": False,
    "skill_comparison": "successful prospective pairs only; retain full denominator",
}


class LoopBlocked(RuntimeError):
    """Only fixed diagnostic codes enter the public report."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def digest(value: Any) -> str:
    return hashlib.sha256(pf.canonical(value).encode()).hexdigest()


def implementation_digest() -> str:
    root = Path(__file__).resolve().parent
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in ("research_loop.py", "prospective_forecast.py", "researched_forecast.py", "safe_smoke_test.py")})


def read_json(path: Path) -> Any:
    if path.stat().st_size > 256 * 1024:
        raise LoopBlocked("file_too_large")
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_manifest(raw: dict, now: datetime) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise LoopBlocked("invalid_manifest")
    if (not isinstance(raw.get("experiment_id"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", raw["experiment_id"])
            or raw.get("mode") not in {"development", "prospective"}
            or pf.parse_utc(raw.get("frozen_at_utc")) > now):
        raise LoopBlocked("invalid_experiment_identity")
    tasks = raw.get("questions")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 20:
        raise LoopBlocked("freeze_one_to_twenty_questions")
    clean = [pf.sanitize_question(q) for q in tasks]
    if len({q["question_id"] for q in clean}) != len(clean):
        raise LoopBlocked("duplicate_question")
    if len({q["url"] for q in clean}) != len(clean):
        raise LoopBlocked("duplicate_question_url")
    for question in clean:
        # Freeze wording and size-check both tool-free and research requests.
        build_request("baseline", question, raw["frozen_at_utc"])
        build_request("research", question, raw["frozen_at_utc"])
        if raw["mode"] == "development" and pf.parse_utc(question["event_time_utc"]) >= now:
            raise LoopBlocked("development_fixture_must_be_historical")
    return {"schema_version": 1, "experiment_id": raw["experiment_id"],
            "mode": raw["mode"], "frozen_at_utc": pf.parse_utc(raw["frozen_at_utc"]).isoformat(),
            "questions": clean}


def build_request(stage: str, question: dict, as_of: str,
                  research: dict | None = None) -> dict:
    data = pf.question_input(question, as_of)
    is_research = stage == "research"
    payload = {
        "model": PROTOCOL["research_model" if is_research else "forecast_model"],
        "instructions": rf.RESEARCH_INSTRUCTIONS if is_research else pf.FORECAST_INSTRUCTIONS,
        "input": pf.canonical(data), "store": False, "service_tier": "default",
        "truncation": "disabled", "max_output_tokens": PROTOCOL["max_output_tokens"],
        "reasoning": {"effort": PROTOCOL["research_reasoning" if is_research else "forecast_reasoning"]},
    }
    if is_research:
        payload.update(tools=[{"type": "web_search", "search_context_size": "low",
                               "filters": {"allowed_domains": question["research_allowed_domains"]}}],
                       tool_choice="required", max_tool_calls=PROTOCOL["max_searches"],
                       include=["web_search_call.action.sources"])
    else:
        supplied = {} if research is None else {k: research[k] for k in ("text", "citations")}
        payload["input"] = pf.canonical(data | {"research": supplied})
        schema = copy.deepcopy(rf.FORECAST_SCHEMA)
        if stage == "baseline":
            schema["properties"]["source_urls"]["maxItems"] = 0
        else:
            urls = sorted({s["url"] for s in supplied["citations"]})
            if not urls:
                raise LoopBlocked("missing_research_citations")
            schema["properties"]["source_urls"].update(
                minItems=1, items={"type": "string", "enum": urls})
        payload.update(tools=[], tool_choice="none", text={"format": {
            "type": "json_schema", "name": "research_loop_forecast_v1", "strict": True,
            "schema": schema}})
    if len(pf.canonical(payload).encode()) > PROTOCOL["max_request_bytes"]:
        raise LoopBlocked("request_too_large")
    return payload


def primary_url(value: Any, domains: list[str]) -> bool:
    if not isinstance(value, str) or len(value) > 4096:
        return False
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        return (url.scheme == "https" and url.username is None and url.password is None
                and url.port is None and all(ord(c) >= 32 and ord(c) != 127 for c in value)
                and any(host == d or host.endswith("." + d) for d in domains))
    except ValueError:
        return False


def response_metadata(data: dict, model: str) -> dict:
    usage = data.get("usage") or {}
    values = (usage.get("input_tokens"), usage.get("output_tokens"))
    complete = all(type(n) is int and n >= 0 for n in values)
    output = data.get("output")
    searches = sum(isinstance(x, dict) and x.get("type") == "web_search_call"
                   for x in output) if isinstance(output, list) else 0
    cost = None
    if complete:
        rate_in, rate_out = map(Decimal, PROTOCOL["prices_per_million"][model])
        # A conservative estimate: no cache discount, with a search-input allowance.
        cost = str(((values[0] + searches * PROTOCOL["search_input_allowance_tokens"]) * rate_in
                    + values[1] * rate_out) / 1_000_000
                   + searches * Decimal(PROTOCOL["search_call_usd"]))
    return {"response_id": data.get("id") if isinstance(data.get("id"), str) else None,
            "returned_model": data.get("model") if isinstance(data.get("model"), str) else None,
            "usage": {"input_tokens": values[0], "output_tokens": values[1]} if complete else None,
            "search_calls": searches, "estimated_cost_usd": cost}


def parse_response(data: dict, payload: dict, question: dict, stage: str,
                   research: dict | None) -> dict:
    expected_model = payload["model"]
    actual_model = data.get("model")
    if (data.get("status") != "completed" or data.get("error") is not None
            or data.get("incomplete_details") is not None
            or not isinstance(data.get("id"), str) or not data["id"]
            or not isinstance(actual_model, str)
            or not re.fullmatch(re.escape(expected_model) + r"(?:-\d{4}-\d{2}-\d{2})?", actual_model)):
        raise LoopBlocked("incomplete_or_unexpected_response")
    metadata = response_metadata(data, expected_model)
    if metadata["usage"] is None or metadata["usage"]["output_tokens"] > PROTOCOL["max_output_tokens"]:
        raise LoopBlocked("invalid_usage")
    if not isinstance(data.get("output"), list):
        raise LoopBlocked("invalid_output")
    texts, citations, sources = [], [], []
    searches = 0
    offset = 0
    for item in data["output"]:
        if not isinstance(item, dict):
            raise LoopBlocked("invalid_output")
        kind = item.get("type")
        if kind == "reasoning":
            continue  # Do not export private reasoning items.
        if kind == "web_search_call":
            if stage != "research" or item.get("status") != "completed":
                raise LoopBlocked("unexpected_tool_call")
            searches += 1
            action = item.get("action") or {}
            if action.get("type") not in {"search", "open_page", "find_in_page"}:
                raise LoopBlocked("unexpected_search_action")
            for source in action.get("sources") or []:
                if isinstance(source, dict) and isinstance(source.get("url"), str):
                    sources.append(source["url"])
        elif kind == "message":
            if item.get("role") != "assistant" or item.get("status") != "completed":
                raise LoopBlocked("incomplete_message")
            for part in item.get("content", []):
                if not isinstance(part, dict) or part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise LoopBlocked("refusal_or_nontext_output")
                text = part["text"]
                for annotation in part.get("annotations", []):
                    if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                        continue
                    start, end = annotation.get("start_index"), annotation.get("end_index")
                    url = annotation.get("url")
                    if (not primary_url(url, question["research_allowed_domains"])
                            or type(start) is not int or type(end) is not int
                            or not 0 <= start < end <= len(text)):
                        raise LoopBlocked("invalid_citation")
                    citations.append({"url": url, "start_index": start + offset,
                                      "end_index": end + offset, "cited_text": text[start:end]})
                texts.append(text)
                offset += len(text) + 1
        else:
            raise LoopBlocked("unexpected_output_type")
    text = "\n".join(texts)
    if not text.strip():
        raise LoopBlocked("empty_output")
    if stage == "research":
        if not 1 <= searches <= PROTOCOL["max_searches"] or not citations:
            raise LoopBlocked("research_missing_search_or_citations")
        if any(not primary_url(url, question["research_allowed_domains"]) for url in sources):
            raise LoopBlocked("retrieved_source_outside_domains")
        return {"text": text, "citations": citations, "retrieved_urls": sorted(set(sources))}
    known = set() if research is None else {s["url"] for s in research["citations"]}
    try:
        forecast = rf._parse_forecast(text, known, allow_empty_sources=stage == "baseline")
    except rf.ResearchBlocked:
        raise LoopBlocked("forecast_schema_or_provenance_failed") from None
    return forecast


class Ledger:
    def __init__(self, path: Path, manifest: dict, budget: Decimal):
        if not budget.is_finite() or not Decimal("0") < budget <= Decimal("100"):
            raise LoopBlocked("budget_must_be_positive_and_at_most_100")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS definition (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
                question_id TEXT, stage TEXT, status TEXT NOT NULL,
                reserved_usd TEXT NOT NULL, started_at_utc TEXT NOT NULL,
                completed_at_utc TEXT, payload_sha256 TEXT NOT NULL,
                value TEXT, metadata TEXT, failure_code TEXT,
                PRIMARY KEY (question_id, stage));
        """)
        definition = {"manifest": manifest, "protocol": PROTOCOL,
                      "implementation_sha256": implementation_digest(),
                      "budget_usd": str(budget.normalize())}
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO definition VALUES (1, ?)", (pf.canonical(definition),))
        if json.loads(self.db.execute("SELECT value FROM definition WHERE id=1").fetchone()[0]) != definition:
            self.db.close()
            raise LoopBlocked("ledger_definition_changed")
        self.manifest, self.budget = manifest, budget

    def close(self) -> None:
        self.db.close()

    def attempt(self, question_id: str, stage: str) -> dict | None:
        row = self.db.execute("SELECT * FROM attempts WHERE question_id=? AND stage=?",
                              (question_id, stage)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("value", "metadata"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def reserve(self, question_id: str, stage: str, payload: dict, timestamp: str) -> None:
        reservation = Decimal(PROTOCOL["reservation_per_request_usd"])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.attempt(question_id, stage) is not None:
                raise LoopBlocked("attempt_already_recorded")
            rows = self.db.execute("SELECT reserved_usd, metadata FROM attempts").fetchall()
            consumed = Decimal("0")
            for row in rows:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
                observed = Decimal(meta["estimated_cost_usd"]) if meta.get("estimated_cost_usd") is not None else Decimal("0")
                consumed += max(Decimal(row["reserved_usd"]), observed)
            if consumed + reservation > self.budget:
                raise LoopBlocked("reservation_budget_exhausted")
            self.db.execute("INSERT INTO attempts (question_id, stage, status, reserved_usd, started_at_utc, payload_sha256) VALUES (?, ?, 'in_flight', ?, ?, ?)",
                            (question_id, stage, str(reservation), timestamp, digest(payload)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def check_remaining_budget(self) -> None:
        attempts = [self.attempt(q["question_id"], s) for q in self.manifest["questions"] for s in STAGES]
        if any(a is not None and a["status"] != "completed" for a in attempts):
            raise LoopBlocked("prior_failed_or_ambiguous_attempt_requires_review")
        consumed = Decimal("0")
        for attempt in attempts:
            if attempt is None:
                consumed += Decimal(PROTOCOL["reservation_per_request_usd"])
            else:
                meta = attempt["metadata"] or {}
                observed = Decimal(meta["estimated_cost_usd"]) if meta.get("estimated_cost_usd") is not None else Decimal("0")
                consumed += max(Decimal(attempt["reserved_usd"]), observed)
        if consumed > self.budget:
            raise LoopBlocked("full_campaign_exceeds_reservation_budget")

    def finish(self, question_id: str, stage: str, *, value: dict | None,
               metadata: dict | None, code: str | None, completed_at: str) -> None:
        with self.db:
            self.db.execute("UPDATE attempts SET status=?, completed_at_utc=?, value=?, metadata=?, failure_code=? WHERE question_id=? AND stage=? AND status='in_flight'",
                            ("completed" if code is None else "failed", completed_at,
                             pf.canonical(value) if value is not None else None,
                             pf.canonical(metadata) if metadata is not None else None,
                             code, question_id, stage))

    def report(self) -> dict:
        records = []
        for question in self.manifest["questions"]:
            stages = {s: self.attempt(question["question_id"], s) for s in STAGES}
            completed = all(v is not None and v["status"] == "completed" for v in stages.values())
            start, end = stages["baseline"], stages["forecast"]
            timely = False
            if completed:
                timely = (pf.parse_utc(end["completed_at_utc"]) - pf.parse_utc(start["started_at_utc"])).total_seconds() <= PROTOCOL["max_pair_seconds"]
                if self.manifest["mode"] == "prospective":
                    timely = timely and pf.parse_utc(question["event_time_utc"]) - pf.parse_utc(end["completed_at_utc"]) > timedelta(seconds=PROTOCOL["minimum_lead_seconds"])
            records.append({"question": question, "stages": stages,
                            "completed_pair": completed, "timing_valid": timely,
                            "eligible_for_skill_scoring": completed and timely and self.manifest["mode"] == "prospective"})
        attempts = [a for record in records for a in record["stages"].values() if a is not None]
        return {"schema_version": 1, "experiment_id": self.manifest["experiment_id"],
                "mode": self.manifest["mode"], "manifest_sha256": digest(self.manifest),
                "protocol_sha256": digest(PROTOCOL), "protocol": PROTOCOL,
                "implementation_sha256": implementation_digest(),
                "budget_usd": str(self.budget), "budget_is_provider_billing_cap": False,
                "request_attempts": len(attempts),
                "reserved_usd": str(sum((Decimal(a["reserved_usd"]) for a in attempts), Decimal("0"))),
                "known_estimated_cost_usd": str(sum((Decimal(a["metadata"]["estimated_cost_usd"]) for a in attempts
                    if a["metadata"] and a["metadata"]["estimated_cost_usd"] is not None), Decimal("0"))),
                "consumed_allowance_usd": str(sum((max(Decimal(a["reserved_usd"]),
                    Decimal(a["metadata"]["estimated_cost_usd"]) if a["metadata"] and a["metadata"]["estimated_cost_usd"] is not None else Decimal("0"))
                    for a in attempts), Decimal("0"))),
                "usage_complete": all(a["metadata"] and a["metadata"]["usage"] is not None for a in attempts),
                "completed_pairs": sum(r["completed_pair"] for r in records),
                "eligible_pairs": sum(r["eligible_for_skill_scoring"] for r in records),
                "forecast_skill": "not_evaluated", "records": records}


def save_report(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


async def run_loop(ledger: Ledger, client: Any, *, checkpoint: Callable[[dict], None],
                   clock: Callable[[], datetime] = now_utc) -> dict:
    checkpoint(ledger.report())
    if clock() >= pf.parse_utc(PROTOCOL["pricing_review_deadline"]):
        raise LoopBlocked("pricing_review_required")
    ledger.check_remaining_budget()
    if ledger.manifest["mode"] == "prospective":
        for question in ledger.manifest["questions"]:
            if pf.parse_utc(question["event_time_utc"]) - clock() <= timedelta(seconds=PROTOCOL["minimum_lead_seconds"]):
                # Already-complete ledgers can still be inspected/scored offline.
                if any(ledger.attempt(question["question_id"], s) is None for s in STAGES):
                    raise LoopBlocked("prospective_lead_expired")
    for question in ledger.manifest["questions"]:
        research = None
        baseline_time = None
        for stage in STAGES:
            previous = ledger.attempt(question["question_id"], stage)
            if previous is not None:
                if previous["status"] != "completed":
                    raise LoopBlocked("prior_failed_or_ambiguous_attempt_requires_review")
                if stage == "baseline":
                    baseline_time = previous["started_at_utc"]
                elif stage == "research":
                    research = previous["value"]
                continue
            timestamp = clock()
            if ledger.manifest["mode"] == "prospective":
                if pf.parse_utc(question["event_time_utc"]) - timestamp <= timedelta(seconds=PROTOCOL["minimum_lead_seconds"]):
                    raise LoopBlocked("prospective_lead_expired")
            if baseline_time is not None and (timestamp - pf.parse_utc(baseline_time)).total_seconds() >= PROTOCOL["max_pair_seconds"]:
                raise LoopBlocked("pair_deadline_expired")
            baseline_time = baseline_time or timestamp.isoformat()
            payload = build_request(stage, question, baseline_time, research)
            ledger.reserve(question["question_id"], stage, payload, timestamp.isoformat())
            checkpoint(ledger.report())
            metadata, value, code = None, None, None
            try:
                response = await client.responses.create(**payload)
                data = response.model_dump(mode="json")
                metadata = response_metadata(data, payload["model"])
                value = parse_response(data, payload, question, stage, research)
            except LoopBlocked as exc:
                code = str(exc)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                code = "provider_http_" + str(status) if type(status) is int and 100 <= status <= 599 else "request_failed_or_ambiguous"
            ledger.finish(question["question_id"], stage, value=value, metadata=metadata,
                          code=code, completed_at=clock().isoformat())
            checkpoint(ledger.report())
            if code:
                raise LoopBlocked(code)
            if stage == "research":
                research = value
    result = ledger.report()
    if any(r["completed_pair"] and not r["timing_valid"] for r in result["records"]):
        raise LoopBlocked("completed_pair_missed_deadline")
    return result


def score(report: dict, outcomes: dict) -> dict:
    """Score only eligible prospective pairs; label file is never model input."""
    if report["mode"] != "prospective":
        raise LoopBlocked("development_is_not_forecast_skill")
    if not isinstance(outcomes, dict) or outcomes.get("schema_version") != 1:
        raise LoopBlocked("invalid_outcomes")
    ids = {r["question"]["question_id"] for r in report["records"]}
    labels = outcomes.get("outcomes")
    if not isinstance(labels, list) or any(not isinstance(x, dict) for x in labels):
        raise LoopBlocked("invalid_outcomes")
    if len({x.get("question_id") for x in labels}) != len(labels):
        raise LoopBlocked("duplicate_outcome")
    by_id = {}
    for label in labels:
        if label.get("question_id") not in ids or type(label.get("outcome")) is not int or label["outcome"] not in (0, 1):
            raise LoopBlocked("invalid_outcome_identity_or_value")
        if not rf._url(label.get("source_url")):
            raise LoopBlocked("outcome_source_required")
        pf.parse_utc(label.get("resolved_at_utc"))
        by_id[label["question_id"]] = label
    pairs = []
    for record in report["records"]:
        label = by_id.get(record["question"]["question_id"])
        if not record["eligible_for_skill_scoring"] or label is None:
            continue
        if pf.parse_utc(label["resolved_at_utc"]) <= pf.parse_utc(record["stages"]["forecast"]["completed_at_utc"]):
            raise LoopBlocked("outcome_predates_forecast")
        y = label["outcome"]
        probabilities = {a: record["stages"][s]["value"]["probability_yes"]
                         for a, s in (("baseline", "baseline"), ("researched", "forecast"))}
        brier = {a: (p - y) ** 2 for a, p in probabilities.items()}
        logloss = {a: ("infinite" if (p if y else 1 - p) == 0 else -math.log(p if y else 1 - p))
                   for a, p in probabilities.items()}
        pairs.append({"question_id": record["question"]["question_id"], "outcome": label,
                      "brier": brier, "log_loss": logloss,
                      "brier_delta_researched_minus_baseline": brier["researched"] - brier["baseline"]})
    return {"frozen_questions": len(report["records"]), "eligible_pairs": report["eligible_pairs"],
            "scored_pairs": len(pairs), "resolved_questions": len(by_id),
            "mean_brier_delta": sum(p["brier_delta_researched_minus_baseline"] for p in pairs) / len(pairs) if pairs else None,
            "pairs": pairs, "skill_claim": "descriptive pilot only; no demonstrated edge"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("research-loop-development.json"))
    parser.add_argument("--ledger", type=Path, default=Path(".work/research-loop.sqlite"))
    parser.add_argument("--report", type=Path, default=Path(".work/research-loop-report.json"))
    parser.add_argument("--budget-usd", default="1.00")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--initialize", action="store_true")
    modes.add_argument("--score", type=Path, metavar="OUTCOMES")
    args = parser.parse_args()
    ledger = None
    try:
        manifest = normalize_manifest(read_json(args.manifest), now_utc())
        budget = Decimal(args.budget_usd)
        if not budget.is_finite() or not Decimal("0") < budget <= Decimal("100"):
            raise LoopBlocked("invalid_budget")
        if not (args.execute or args.initialize or args.score):
            print(json.dumps({"status": "offline_plan", "experiment_id": manifest["experiment_id"],
                              "questions": len(manifest["questions"]), "models": [PROTOCOL["research_model"], PROTOCOL["forecast_model"]],
                              "manifest_sha256": digest(manifest), "protocol_sha256": digest(PROTOCOL),
                              "max_requests": len(manifest["questions"]) * 3,
                              "full_campaign_reservation_usd": str(Decimal("0.75") * len(manifest["questions"])),
                              "full_campaign_fits_budget": Decimal("0.75") * len(manifest["questions"]) <= budget,
                              "budget_usd": str(budget), "paid_requests": 0}, indent=2))
            return
        ledger = Ledger(args.ledger, manifest, budget)
        checkpoint = lambda value: save_report(args.report, value)
        checkpoint(ledger.report())
        if args.score:
            result = score(ledger.report(), read_json(args.score))
            save_report(args.report.with_name("research-loop-scores.json"), result)
            print(json.dumps(result, indent=2))
            return
        if args.execute:
            if not os.getenv("OPENAI_API_KEY"):
                raise LoopBlocked("openai_api_key_missing")
            from openai import AsyncOpenAI
            async def execute():
                async with AsyncOpenAI(base_url=rf.API_BASE, max_retries=0, timeout=120) as client:
                    return await run_loop(ledger, client, checkpoint=checkpoint)
            asyncio.run(execute())
        result = ledger.report()
        print(json.dumps({k: result[k] for k in ("experiment_id", "mode", "request_attempts",
                          "completed_pairs", "eligible_pairs", "reserved_usd", "known_estimated_cost_usd", "usage_complete")}, indent=2))
    except (LoopBlocked, ValueError, OSError) as exc:
        if ledger is not None:
            save_report(args.report, ledger.report())
        code = str(exc) if isinstance(exc, LoopBlocked) else "invalid_local_input"
        raise SystemExit("Research loop stopped: " + code) from None
    finally:
        if ledger is not None:
            ledger.close()


if __name__ == "__main__":
    main()
