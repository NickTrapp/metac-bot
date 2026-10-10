"""One synthetic binary forecast, direct OpenAI, no Metaculus access.

No paid work is done by importing this module or running it without --execute.
The supported paid path is the first attempt of the sole GitHub campaign run.
See SAFE_TEST.md for the scope and limitations of the spending safeguards.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from typing import Any
import urllib.request

REPOSITORY = "NickTrapp/metac-bot"
WORKFLOW = "openai_smoke_test.yaml"
MODEL = "gpt-4.1-mini-2025-04-14"
API_BASE = "https://api.openai.com/v1"
MAX_OUTPUT_TOKENS = 2048
MAX_REQUEST_ATTEMPTS = 2
RESERVATION_PER_REQUEST_USD = Decimal("0.50")
TOTAL_AUTHORIZED_USD = Decimal("10.00")
PRICE_RECHECK_AFTER = "2026-10-17"
# Official model page, verified 2026-10-10. Reserve the ENTIRE context window,
# not an estimated prompt length, plus the allowed output. No tools or images.
CONTEXT_TOKENS = 1_047_576
INPUT_USD_PER_MILLION = Decimal("0.40")
OUTPUT_USD_PER_MILLION = Decimal("1.60")


def verify_campaign_run(env: dict[str, str], run_history: dict[str, Any]) -> None:
    """Fail closed, including on missing/incomplete history and re-runs."""
    if env.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise RuntimeError("Paid execution is restricted to the approved repository.")
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise RuntimeError("Only the manual initial test is supported.")
    if env.get("GITHUB_RUN_ATTEMPT") != "1":
        raise RuntimeError("Re-runs are blocked; reconcile prior spend before another test.")
    if env.get("GITHUB_RUN_NUMBER") != "1":
        raise RuntimeError("Only campaign run number one is allowed, even if old history was deleted.")
    if env.get("GITHUB_WORKFLOW_REF") != f"{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/main":
        raise RuntimeError("Paid execution must use the reviewed main-branch workflow.")
    runs = run_history.get("workflow_runs")
    if run_history.get("total_count") != 1 or not isinstance(runs, list) or len(runs) != 1:
        raise RuntimeError("Campaign already attempted or history is incomplete; no paid calls allowed.")
    if str(runs[0].get("id")) != env.get("GITHUB_RUN_ID"):
        raise RuntimeError("Cannot establish that this is the first campaign run.")


def check_paid_gate(env: dict[str, str]) -> None:
    if datetime.now(timezone.utc).date().isoformat() > PRICE_RECHECK_AFTER:
        raise RuntimeError("Recheck model availability and prices before another paid test.")
    if env.get("INITIAL_OPENAI_TEST_APPROVED") != "true":
        raise RuntimeError("The initial paid test has not been selected.")
    token = env.get("GH_TOKEN")
    if not token:
        raise RuntimeError("Missing read-only Actions history access; paid calls blocked.")
    url = f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/{WORKFLOW}/runs?per_page=1"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            history = json.load(response)
    except Exception:
        raise RuntimeError("Cannot verify campaign history; paid calls blocked.") from None
    verify_campaign_run(env, history)


@dataclass
class RequestBudget:
    """Per-campaign counter; one-shot history gate provides cross-run protection."""
    attempts: int = 0
    observed_token_cost: Decimal = Decimal("0")

    def reserve(self) -> None:
        bound = (CONTEXT_TOKENS * INPUT_USD_PER_MILLION
                 + MAX_OUTPUT_TOKENS * OUTPUT_USD_PER_MILLION) / 1_000_000
        if bound > RESERVATION_PER_REQUEST_USD:
            raise RuntimeError("Price assumptions exceed the reservation; paid calls blocked.")
        if self.attempts >= MAX_REQUEST_ATTEMPTS:
            raise RuntimeError("Initial-test request allowance exhausted; retries are blocked.")
        if (self.attempts + 1) * RESERVATION_PER_REQUEST_USD > TOTAL_AUTHORIZED_USD:
            raise RuntimeError("The cumulative initial-test authorization is exhausted.")
        # Synchronous increment BEFORE the first await or outbound request.
        # Errors, timeouts, empty replies, and parse failures never refund it.
        self.attempts += 1


def make_guarded_llm(base_class: type, client: Any, budget: RequestBudget) -> Any:
    """Use the official bot's parsing interface but bypass provider fallback/retries."""
    class InitialTestLlm(base_class):
        def __init__(self) -> None:
            super().__init__(model=f"openai/{MODEL}", allowed_tries=1,
                             temperature=0.2, max_tokens=MAX_OUTPUT_TOKENS)

        async def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
            if not isinstance(prompt, str) or (system_prompt is not None and not isinstance(system_prompt, str)):
                raise ValueError("This initial test accepts text-only prompts.")
            messages = [{"role": "user", "content": prompt}]
            if system_prompt is not None:
                messages.insert(0, {"role": "system", "content": system_prompt})
            budget.reserve()
            try:
                result = await client.chat.completions.create(
                    model=MODEL, messages=messages, max_tokens=MAX_OUTPUT_TOKENS,
                    temperature=0.2, n=1, stream=False, service_tier="default",
                )
            except Exception:
                # SDK errors may contain response bodies or credential fragments.
                # GitHub exact-secret masking is not a substitute for sanitizing.
                raise RuntimeError("OpenAI request failed; attempt remains reserved and no automatic retry is allowed.") from None
            # Do not log SDK response objects, headers, credentials, or prompts.
            if result.model != MODEL:
                raise RuntimeError("Unexpected response model; stop the initial test.")
            usage = result.usage
            if usage is None:
                raise RuntimeError("Missing usage; stop rather than retry an unpriced request.")
            if not (0 <= usage.prompt_tokens <= CONTEXT_TOKENS and
                    0 <= usage.completion_tokens <= MAX_OUTPUT_TOKENS):
                raise RuntimeError("Usage exceeds the reviewed bounds; stop the initial test.")
            budget.observed_token_cost += (
                usage.prompt_tokens * INPUT_USD_PER_MILLION
                + usage.completion_tokens * OUTPUT_USD_PER_MILLION
            ) / 1_000_000
            choice = result.choices[0]
            if choice.finish_reason != "stop" or not choice.message.content:
                raise RuntimeError("Incomplete/empty model output; no automatic retry.")
            return choice.message.content

        async def invoke_and_return_verified_type(self, prompt: Any, output_type: Any,
                                                  allowed_invoke_tries_for_failed_output: int = 1) -> Any:
            # Upstream structure_output defaults to three parsing retries. Force one.
            return await super().invoke_and_return_verified_type(
                prompt, output_type, allowed_invoke_tries_for_failed_output=1)

    return InitialTestLlm()


class NoMetaculusAccess:
    def __getattr__(self, name: str) -> Any:
        raise RuntimeError("Metaculus API access is disabled in this synthetic test.")


async def run_paid_test() -> dict[str, Any]:
    # Imports happen only AFTER the gate, never during offline checks.
    import logging
    logging.disable(logging.CRITICAL)
    from openai import AsyncOpenAI
    from forecasting_tools import BinaryQuestion, GeneralLlm
    from main import FallTemplateBot2026

    budget = RequestBudget()
    async with AsyncOpenAI(base_url=API_BASE, max_retries=0, timeout=45) as client:
        llm = make_guarded_llm(GeneralLlm, client, budget)
        bot = FallTemplateBot2026(
            research_reports_per_question=1, predictions_per_research_report=1,
            use_research_summary_to_forecast=False, enable_summarize_research=False,
            publish_reports_to_metaculus=False, folder_to_save_reports_to=None,
            skip_previously_forecasted_questions=False,
            metaculus_client=NoMetaculusAccess(),
            llms={"default": llm, "parser": llm, "summarizer": llm, "researcher": "no_research"},
        )
        bot._structure_output_validation_samples = 1
        question = BinaryQuestion(
            question_text="Will a single fair coin flip tomorrow land heads?",
            background_info="Synthetic connectivity test. The coin is fair and independent. No real event or market is involved.",
            resolution_criteria="Resolves Yes for heads and No for tails in the hypothetical single flip.",
            fine_print="Assume no edge landing, no selection bias, and no hidden information.",
        )
        # Exercise the official baseline's binary prompt + typed-parser path.
        # No tournament retrieval, report publishing, research, or real question ID.
        prediction = await bot._run_forecast_on_binary(question, "No external research; synthetic fair coin.")
    return {"test": "synthetic binary", "provider": "OpenAI", "model": MODEL,
            "published": False, "request_attempts": budget.attempts,
            "reserved_usd": str(budget.attempts * RESERVATION_PER_REQUEST_USD),
            "observed_token_cost_usd": str(budget.observed_token_cost),
            "probability_yes": prediction.prediction_value}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("Offline only. Run python -m unittest discover -s tests -v. See SAFE_TEST.md.")
        return
    check_paid_gate(dict(os.environ))
    try:
        result = asyncio.run(run_paid_test())
    except Exception:
        raise SystemExit("Initial synthetic test failed. No automatic retry is permitted. Review safe diagnostics and cumulative spend before any new test.") from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
