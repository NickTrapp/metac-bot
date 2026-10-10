import asyncio
import ast
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import safe_smoke_test as safe

ROOT = Path(__file__).resolve().parents[1]


def campaign_env():
    return {
        "GITHUB_REPOSITORY": safe.REPOSITORY,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_NUMBER": "1",
        "GITHUB_RUN_ID": "123",
        "GITHUB_WORKFLOW_REF": f"{safe.REPOSITORY}/.github/workflows/{safe.WORKFLOW}@refs/heads/main",
    }


class CampaignTests(unittest.TestCase):
    def test_only_first_current_run_is_allowed(self):
        safe.verify_campaign_run(campaign_env(), {"total_count": 1, "workflow_runs": [{"id": 123}]})

    def test_prior_incomplete_and_wrong_history_are_blocked(self):
        for history in [{}, {"total_count": 0},
                        {"total_count": 2, "workflow_runs": [{"id": 123}]},
                        {"total_count": 1, "workflow_runs": []},
                        {"total_count": 1, "workflow_runs": [{"id": 999}]}]:
            with self.subTest(history=history), self.assertRaises(RuntimeError):
                safe.verify_campaign_run(campaign_env(), history)

    def test_reruns_branches_forks_and_automatic_events_are_blocked(self):
        for key, value in [("GITHUB_RUN_ATTEMPT", "2"), ("GITHUB_RUN_NUMBER", "2"), ("GITHUB_EVENT_NAME", "schedule"),
                           ("GITHUB_REPOSITORY", "other/repo"),
                           ("GITHUB_WORKFLOW_REF", "refs/heads/other")]:
            env = campaign_env(); env[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                safe.verify_campaign_run(env, {"total_count": 1, "workflow_runs": [{"id": 123}]})

    def test_missing_gate_inputs_do_not_request_history(self):
        with patch.object(safe.urllib.request, "urlopen") as request:
            with self.assertRaises(RuntimeError):
                safe.check_paid_gate({})
            request.assert_not_called()

    def test_unavailable_history_is_fail_closed(self):
        env = campaign_env() | {"INITIAL_OPENAI_TEST_APPROVED": "true", "GH_TOKEN": "dummy-test-only"}
        with patch.object(safe, "PRICE_RECHECK_AFTER", "9999-12-31"):
            with patch.object(safe.urllib.request, "urlopen", side_effect=OSError("offline")):
                with self.assertRaisesRegex(RuntimeError, "Cannot verify campaign history"):
                    safe.check_paid_gate(env)


class FakeGeneralLlm:
    def __init__(self, **kwargs):
        self.configuration = kwargs

    async def invoke_and_return_verified_type(self, prompt, output_type, **kwargs):
        return kwargs


def completion(content="Probability: 50%", model=safe.MODEL, finish_reason="stop", usage=True):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20) if usage else None,
    )


def llm_fixture(**mock_kwargs):
    create = AsyncMock(**mock_kwargs)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    budget = safe.RequestBudget()
    return safe.make_guarded_llm(FakeGeneralLlm, client, budget), create, budget


class BudgetAndTransportTests(unittest.IsolatedAsyncioTestCase):
    def test_full_context_reservation_not_token_guess(self):
        bound = (safe.CONTEXT_TOKENS * safe.INPUT_USD_PER_MILLION
                 + safe.MAX_OUTPUT_TOKENS * safe.OUTPUT_USD_PER_MILLION) / 1_000_000
        self.assertEqual(bound, Decimal("0.4223072"))
        self.assertLessEqual(bound, safe.RESERVATION_PER_REQUEST_USD)
        self.assertEqual(safe.MAX_REQUEST_ATTEMPTS * safe.RESERVATION_PER_REQUEST_USD, Decimal("1.00"))
        self.assertLessEqual(Decimal("1.00"), safe.TOTAL_AUTHORIZED_USD)

    async def test_pinned_model_max_tokens_and_single_completion(self):
        llm, create, budget = llm_fixture(return_value=completion())
        self.assertEqual(await llm.invoke("test"), "Probability: 50%")
        self.assertEqual(budget.attempts, 1)
        self.assertEqual(budget.observed_token_cost, Decimal("0.000072"))
        self.assertEqual(create.call_args.kwargs, {
            "model": safe.MODEL, "messages": [{"role": "user", "content": "test"}],
            "max_tokens": 2048, "temperature": 0.2, "n": 1,
            "stream": False, "service_tier": "default",
        })

    async def test_reservation_occurs_before_send_and_failure_is_not_refunded(self):
        llm, create, budget = llm_fixture()
        async def fail(**kwargs):
            self.assertGreater(budget.attempts, 0)
            raise TimeoutError("ambiguous failure")
        create.side_effect = fail
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "OpenAI request failed"):
                await llm.invoke("test")
        with self.assertRaises(RuntimeError):
            await llm.invoke("retry")
        self.assertEqual(create.await_count, 2)
        self.assertEqual(budget.attempts, 2)

    async def test_sdk_error_details_are_not_exposed(self):
        llm, _, _ = llm_fixture(side_effect=RuntimeError("credential-fragment-do-not-print"))
        with self.assertRaises(RuntimeError) as caught:
            await llm.invoke("test")
        self.assertNotIn("credential-fragment", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    async def test_concurrent_calls_cannot_exceed_allowance(self):
        llm, create, budget = llm_fixture(return_value=completion())
        results = await asyncio.gather(*(llm.invoke("test") for _ in range(3)), return_exceptions=True)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(sum(isinstance(r, RuntimeError) for r in results), 1)
        self.assertEqual(budget.attempts, 2)

    async def test_rejects_nontext_before_reserving(self):
        llm, create, budget = llm_fixture(return_value=completion())
        with self.assertRaises(ValueError):
            await llm.invoke([{"role": "user", "content": "image"}])
        create.assert_not_called()
        self.assertEqual(budget.attempts, 0)

    async def test_rejects_unverified_results_without_refund(self):
        for result in [completion(model="unexpected"), completion(usage=False),
                       completion(content=""), completion(finish_reason="length")]:
            with self.subTest(result=result):
                llm, create, budget = llm_fixture(return_value=result)
                with self.assertRaises(RuntimeError):
                    await llm.invoke("test")
                self.assertEqual(budget.attempts, 1)
                self.assertEqual(create.await_count, 1)

    async def test_upstream_parser_retries_are_forced_to_one(self):
        llm, _, _ = llm_fixture(return_value=completion())
        result = await llm.invoke_and_return_verified_type("test", dict, allowed_invoke_tries_for_failed_output=99)
        self.assertEqual(result["allowed_invoke_tries_for_failed_output"], 1)

    def test_metaculus_client_is_unusable(self):
        with self.assertRaises(RuntimeError):
            safe.NoMetaculusAccess().post_question_prediction


class RepositorySafetyTests(unittest.TestCase):
    def test_no_schedule_in_any_workflow(self):
        for path in (ROOT / ".github/workflows").glob("*.yaml"):
            self.assertNotRegex(path.read_text(), r"(?m)^\s+(schedule|cron):", str(path))

    def test_paid_workflow_has_only_openai_secret_and_serialization(self):
        workflow = (ROOT / ".github/workflows/openai_smoke_test.yaml").read_text()
        self.assertNotIn("METACULUS_TOKEN", workflow)
        self.assertNotIn("OPENROUTER_API_KEY", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("group: metaculus-initial-openai-campaign", workflow)
        self.assertNotIn("contents: write", workflow)

    def test_legacy_entrypoints_fail_before_importing_dependencies(self):
        for filename in ["main.py", "main_with_no_framework.py"]:
            run = subprocess.run([sys.executable, str(ROOT / filename)], capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("disabled", run.stderr.lower())
            self.assertNotIn("ModuleNotFoundError", run.stderr)

    def test_default_entrypoint_is_offline(self):
        run = subprocess.run([sys.executable, str(ROOT / "safe_smoke_test.py")], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0)
        self.assertIn("Offline only", run.stdout)

    def test_all_official_question_handlers_retained(self):
        tree = ast.parse((ROOT / "main.py").read_text())
        bot = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FallTemplateBot2026")
        methods = {n.name for n in bot.body if isinstance(n, ast.AsyncFunctionDef)}
        for kind in ["binary", "multiple_choice", "numeric", "date", "conditional"]:
            self.assertIn(f"_run_forecast_on_{kind}", methods)

    def test_sdk_retries_disabled_and_direct_endpoint_fixed(self):
        tree = ast.parse((ROOT / "safe_smoke_test.py").read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "AsyncOpenAI"]
        self.assertEqual(len(calls), 1)
        args = {kw.arg: kw.value for kw in calls[0].keywords}
        self.assertEqual(ast.literal_eval(args["max_retries"]), 0)
        self.assertEqual(args["base_url"].id, "API_BASE")
        self.assertEqual(safe.API_BASE, "https://api.openai.com/v1")


if __name__ == "__main__":
    unittest.main()
