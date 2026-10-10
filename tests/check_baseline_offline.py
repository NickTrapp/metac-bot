"""Dependency-backed check: real baseline/parser classes, fake SDK, socket guard.

Run after poetry install: poetry run python tests/check_baseline_offline.py
No credentials are needed. This is deliberately separate from stdlib unit tests.
"""
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
logging.disable(logging.CRITICAL)


def deny_network(*args, **kwargs):
    raise AssertionError("Network is forbidden in the dependency-backed offline check")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

from forecasting_tools import (BinaryQuestion, ConditionalQuestion, DateQuestion,
                               GeneralLlm, MultipleChoiceQuestion, NumericQuestion,
                               ReasonedPrediction)
from main import FallTemplateBot2026
from safe_smoke_test import MODEL, NoMetaculusAccess, RequestBudget, make_guarded_llm


def fake_completion(content):
    return SimpleNamespace(model=MODEL,
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))])


def make_bot(parsed):
    responses = [fake_completion("Synthetic forecast rationale."), fake_completion(json.dumps(parsed))]
    create = AsyncMock(side_effect=responses)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    budget = RequestBudget()
    llm = make_guarded_llm(GeneralLlm, client, budget)
    bot = FallTemplateBot2026(
        research_reports_per_question=1, predictions_per_research_report=1,
        enable_summarize_research=False, publish_reports_to_metaculus=False,
        metaculus_client=NoMetaculusAccess(),
        llms={"default": llm, "parser": llm, "summarizer": llm, "researcher": "no_research"})
    bot._structure_output_validation_samples = 1
    return bot, create, budget


class BaselineOfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_binary_real_parser(self):
        bot, create, budget = make_bot({"prediction_in_decimal": 0.5})
        result = await bot._run_forecast_on_binary(BinaryQuestion(question_text="Synthetic fair coin?"), "")
        self.assertEqual(result.prediction_value, 0.5)
        self.assertEqual(create.await_count, 2)
        self.assertEqual(budget.attempts, 2)

    async def test_invalid_binary_does_not_retry(self):
        bot, create, budget = make_bot({"prediction_in_decimal": 2.5})
        with self.assertRaises(Exception):
            await bot._run_forecast_on_binary(BinaryQuestion(question_text="Synthetic fair coin?"), "")
        self.assertEqual(create.await_count, 2)
        self.assertEqual(budget.attempts, 2)

    async def test_multiple_choice_real_parser(self):
        bot, create, _ = make_bot({"predicted_options": [
            {"option_name": "A", "probability": 0.4}, {"option_name": "B", "probability": 0.6}]})
        result = await bot._run_forecast_on_multiple_choice(
            MultipleChoiceQuestion(question_text="Synthetic choice?", options=["A", "B"]), "")
        self.assertEqual(result.prediction_value.to_dict(), {"A": 0.4, "B": 0.6})
        self.assertEqual(create.await_count, 2)

    async def test_numeric_real_parser(self):
        parsed = [{"percentile": p, "value": p * 100} for p in [0.1, 0.2, 0.4, 0.6, 0.8, 0.9]]
        bot, create, _ = make_bot(parsed)
        result = await bot._run_forecast_on_numeric(NumericQuestion(
            question_text="Synthetic number?", lower_bound=0, upper_bound=100,
            open_lower_bound=False, open_upper_bound=False, unit_of_measure="units"), "")
        self.assertEqual(len(result.prediction_value.declared_percentiles), 6)
        self.assertEqual(create.await_count, 2)

    async def test_date_real_parser(self):
        parsed = [{"percentile": p, "value": f"2027-01-{day:02d}T00:00:00Z"}
                  for p, day in [(0.1, 3), (0.2, 6), (0.4, 12), (0.6, 18), (0.8, 24), (0.9, 27)]]
        bot, create, _ = make_bot(parsed)
        result = await bot._run_forecast_on_date(DateQuestion(
            question_text="Synthetic date?", lower_bound=datetime(2027, 1, 1, tzinfo=timezone.utc),
            upper_bound=datetime(2027, 1, 31, tzinfo=timezone.utc),
            open_lower_bound=False, open_upper_bound=False), "")
        self.assertEqual(len(result.prediction_value.declared_percentiles), 6)
        self.assertEqual(create.await_count, 2)

    async def test_conditional_composition_without_model_calls(self):
        bot, create, _ = make_bot({})
        question = BinaryQuestion(question_text="Synthetic coin?")
        conditional = ConditionalQuestion(question_text="Synthetic conditional?", parent=question,
                                          child=question, question_yes=question, question_no=question)
        bot._get_question_prediction_info = AsyncMock(return_value=(
            ReasonedPrediction(prediction_value=0.5, reasoning="Synthetic"), ""))
        result = await bot._run_forecast_on_conditional(conditional, "")
        self.assertEqual(result.prediction_value.prediction_yes, 0.5)
        self.assertEqual(bot._get_question_prediction_info.await_count, 4)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
