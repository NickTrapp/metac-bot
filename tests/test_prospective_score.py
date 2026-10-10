"""Synthetic, stdlib-only scoring fixtures. No network or paid provider calls."""

import copy
from decimal import Decimal
from fractions import Fraction
import json
import math
import unittest

import prospective_score as ps


def record(question_id="q1", baseline=0.5, researched=0.5, *, pair_valid=True,
           baseline_status="completed", researched_status="completed"):
    return {
        "question_id": question_id, "pair_valid": pair_valid,
        **{arm: {"status": status, "probability_yes": probability,
                 "operational_probability_yes": probability if status == "completed" else 0.5}
           for arm, status, probability in (
               ("baseline", baseline_status, baseline),
               ("researched", researched_status, researched))},
    }


class MetricTests(unittest.TestCase):
    def test_brier_uses_binary_zero_to_one_scale(self):
        for p, y, expected in ((0, 0, 0), (1, 1, 0), (0, 1, 1), (1, 0, 1),
                               (0.5, 1, 0.25), (0.25, 1, 0.5625), (0.25, 0, 0.0625)):
            with self.subTest(p=p, y=y):
                self.assertEqual(ps.brier_score(p, y), expected)

    def test_log_loss_uses_natural_logarithm(self):
        self.assertEqual(ps.log_loss(0, 0), 0.0)
        self.assertEqual(ps.log_loss(1, 1), 0.0)
        self.assertAlmostEqual(ps.log_loss(0.5, 0), math.log(2))
        self.assertAlmostEqual(ps.log_loss(0.25, 1), math.log(4))
        self.assertAlmostEqual(ps.log_loss(0.75, 0), math.log(4))

    def test_wrong_endpoints_are_actual_float_infinity(self):
        for probability, label in ((0, 1), (1, 0)):
            value = ps.log_loss(probability, label)
            self.assertIs(type(value), float)
            self.assertEqual(value, math.inf)

    def test_no_epsilon_clipping_and_stable_tiny_loss(self):
        smallest_positive_float = math.nextafter(0.0, 1.0)
        self.assertEqual(ps.log_loss(smallest_positive_float, 1),
                         -math.log(smallest_positive_float))
        self.assertGreater(ps.log_loss(smallest_positive_float, 1), 700)
        self.assertEqual(ps.log_loss(smallest_positive_float, 0), smallest_positive_float)
        nearly_one = math.nextafter(1.0, 0.0)
        self.assertEqual(ps.log_loss(nearly_one, 0), -math.log1p(-nearly_one))

    def test_probability_validation_rejects_non_json_and_nonfinite(self):
        values = (True, False, None, "0.5", -0.01, 1.01, math.inf, -math.inf,
                  math.nan, Decimal("0.5"), Fraction(1, 2), [], {}, 1j, 10 ** 1000)
        for function in (ps.brier_score, ps.log_loss):
            for value in values:
                with self.subTest(function=function.__name__, value=repr(value)), \
                        self.assertRaises(ValueError):
                    function(value, 1)

    def test_only_integer_binary_labels_are_accepted(self):
        for function in (ps.brier_score, ps.log_loss):
            for label in (True, False, 0.0, 1.0, 0.5, -1, 2, "1", None,
                          "cancelled", Decimal(1), Fraction(1, 1), [], {}):
                with self.subTest(function=function.__name__, label=repr(label)), \
                        self.assertRaises(ValueError):
                    function(0.5, label)


class ScoreRunTests(unittest.TestCase):
    def test_paired_scores_and_researched_minus_baseline_direction(self):
        result = ps.score_run([record("a", 0.5, 0.75), record("b", 0.5, 0.25)],
                              {"a": 1, "b": 0})
        for name in ("successful_pairs", "operational"):
            summary = result[name]
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["question_ids"], ["a", "b"])
            self.assertEqual(summary["baseline"]["brier"], 0.25)
            self.assertEqual(summary["researched"]["brier"], 0.0625)
            delta = summary["researched_minus_baseline"]
            self.assertEqual(delta["brier"], -0.1875)
            self.assertAlmostEqual(delta["log_loss"], -math.log(1.5))
            self.assertIsNone(delta["log_loss_undefined_reason"])
        self.assertEqual(result["per_question"][0]["successful_pair_difference"]["brier"], -0.1875)
        self.assertEqual(result["difference_direction"], "researched_minus_baseline")

    def test_failure_fallbacks_are_separate_from_successful_pairs(self):
        records = [record("ok", 0.2, 0.8),
                   record("base-fail", None, 0.9, baseline_status="failed"),
                   record("research-fail", 0.8, None, researched_status="failed"),
                   record("both-fail", None, None, baseline_status="failed", researched_status="failed")]
        result = ps.score_run(records, dict.fromkeys([r["question_id"] for r in records], 1))
        self.assertEqual(result["successful_pairs"]["question_ids"], ["ok"])
        self.assertEqual(result["operational"]["count"], 4)
        self.assertEqual(result["failure_counts"],
                         {"baseline": 2, "researched": 2, "either_arm": 3, "both_arms": 1})
        baseline_failure = result["per_question"][1]
        self.assertIsNone(baseline_failure["baseline"]["forecast_scores"])
        self.assertIsNone(baseline_failure["successful_pair_difference"])
        self.assertEqual(baseline_failure["baseline"]["operational_scores"]["brier"], 0.25)
        self.assertEqual(baseline_failure["successful_pair_exclusion_reasons"], ["baseline_failed"])
        self.assertAlmostEqual(result["operational"]["baseline"]["brier"], (0.64 + 0.25 + 0.04 + 0.25) / 4)

    def test_failed_probability_retained_for_audit_but_fallback_scored(self):
        result = ps.score_run([record(baseline=1, baseline_status="failed")], {"q1": 1})
        arm = result["per_question"][0]["baseline"]
        self.assertEqual(arm["probability_yes"], 1)
        self.assertIsNone(arm["forecast_scores"])
        self.assertEqual(arm["operational_scores"]["brier"], 0.25)

    def test_invalid_timing_excluded_from_success_but_included_operationally(self):
        result = ps.score_run([record("valid", 0.5, 0.8),
                               record("late", 0.5, 1, pair_valid=False),
                               record("late-pending", pair_valid=False)], {"valid": 1, "late": 1})
        self.assertEqual(result["successful_pairs"]["question_ids"], ["valid"])
        self.assertEqual(result["operational"]["question_ids"], ["valid", "late"])
        self.assertEqual(result["invalid_timing_ids"], ["late", "late-pending"])
        self.assertEqual(result["counts"]["invalid_timing_pairs"], 2)
        self.assertEqual(result["counts"]["resolved_invalid_timing_pairs"], 1)
        late = result["per_question"][1]
        self.assertEqual(late["successful_pair_exclusion_reasons"], ["invalid_timing_pair"])
        self.assertIsNone(late["successful_pair_difference"])
        self.assertEqual(late["operational_difference"]["brier"], -0.25)

    def test_non_binary_outcomes_retained_and_never_coerced_or_scored(self):
        ids = ["resolved", "missing", "none", "unresolved", "cancelled", "ambiguous"]
        records = [record(question_id) for question_id in ids]
        labels = {"resolved": 0, "none": None, "unresolved": "unresolved",
                  "cancelled": "cancelled", "ambiguous": "ambiguous"}
        result = ps.score_run(records, labels)
        self.assertEqual(result["counts"]["resolved"], 1)
        self.assertEqual(result["counts"]["unresolved"], 3)
        self.assertEqual(result["counts"]["cancelled"], 1)
        self.assertEqual(result["counts"]["ambiguous"], 1)
        self.assertEqual(result["counts"]["missing_labels"], 1)
        self.assertEqual(result["unresolved_ids"], ids[1:])
        for row in result["per_question"][1:]:
            self.assertFalse(row["operational_included"])
            self.assertFalse(row["successful_pair_included"])
            self.assertIsNone(row["operational_difference"])
            for arm in ps.ARMS:
                self.assertIsNone(row[arm]["forecast_scores"])
                self.assertIsNone(row[arm]["operational_scores"])
        self.assertEqual(result["per_question"][0]["outcome_status"], "resolved")
        self.assertEqual(result["per_question"][0]["label"], 0)

    def test_failures_counted_even_without_binary_resolution(self):
        result = ps.score_run([record(baseline=None, baseline_status="failed")], {})
        self.assertEqual(result["failure_counts"]["baseline"], 1)
        self.assertEqual(result["resolved_failure_counts"]["baseline"], 0)
        self.assertEqual(result["counts"]["operational_pairs"], 0)

    def test_infinity_differences_have_honest_sign(self):
        for baseline, researched, expected in ((0, 0.5, -math.inf), (0.5, 0, math.inf)):
            with self.subTest(baseline=baseline, researched=researched):
                result = ps.score_run([record(baseline=baseline, researched=researched)], {"q1": 1})
                self.assertEqual(result["successful_pairs"]["researched_minus_baseline"]["log_loss"], expected)
                self.assertEqual(result["per_question"][0]["successful_pair_difference"]["log_loss"], expected)

    def test_same_question_infinity_minus_infinity_is_undefined_not_nan(self):
        result = ps.score_run([record(baseline=0, researched=0)], {"q1": 1})
        delta = result["per_question"][0]["successful_pair_difference"]
        self.assertEqual(delta["brier"], 0)
        self.assertIsNone(delta["log_loss"])
        self.assertEqual(delta["log_loss_undefined_reason"], "both_log_losses_infinite")
        aggregate = result["successful_pairs"]["researched_minus_baseline"]
        self.assertIsNone(aggregate["log_loss"])
        self.assertEqual(aggregate["log_loss_undefined_reason"], "both_arm_mean_log_losses_infinite")
        self.assertEqual(result["successful_pairs"]["baseline"]["log_loss"], math.inf)
        json.dumps(ps.to_json_safe(result), allow_nan=False)

    def test_opposite_infinite_differences_across_questions_are_undefined(self):
        result = ps.score_run([record("a", 0, 0.5), record("b", 0.5, 0)], {"a": 1, "b": 1})
        deltas = [row["successful_pair_difference"]["log_loss"] for row in result["per_question"]]
        self.assertEqual(deltas, [-math.inf, math.inf])
        summary = result["successful_pairs"]
        self.assertEqual(summary["baseline"]["log_loss"], math.inf)
        self.assertEqual(summary["researched"]["log_loss"], math.inf)
        self.assertIsNone(summary["researched_minus_baseline"]["log_loss"])
        json.dumps(ps.to_json_safe(result), allow_nan=False)

    def test_empty_analysis_has_no_fabricated_zero_score(self):
        for records in ([], [record()]):
            result = ps.score_run(records, {})
            for key in ("successful_pairs", "operational"):
                self.assertEqual(result[key]["count"], 0)
                self.assertEqual(result[key]["baseline"], {"brier": None, "log_loss": None})
                self.assertIsNone(result[key]["researched_minus_baseline"]["brier"])
                self.assertEqual(result[key]["researched_minus_baseline"]["log_loss_undefined_reason"],
                                 "no_scored_pairs")

    def test_inputs_are_not_modified(self):
        records, labels = [record()], {"q1": 1}
        original_records, original_labels = copy.deepcopy(records), copy.deepcopy(labels)
        result = ps.score_run(records, labels)
        result["per_question"][0]["baseline"]["probability_yes"] = 0
        self.assertEqual(records, original_records)
        self.assertEqual(labels, original_labels)

    def test_duplicate_question_ids_are_rejected_even_if_unresolved(self):
        with self.assertRaisesRegex(ValueError, "duplicate question_id"):
            ps.score_run([record(), record()], {})

    def test_invalid_record_shape_or_timing_flag_is_rejected(self):
        for invalid in (None, {}, (), "x"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ps.score_run(invalid, {})
        for invalid_record in (None, [], "x"):
            with self.assertRaises(ValueError):
                ps.score_run([invalid_record], {})
        for key, value in (("question_id", None), ("question_id", 42), ("question_id", ""),
                           ("question_id", "  "), ("pair_valid", 1), ("pair_valid", None),
                           ("pair_valid", "false")):
            bad = record(); bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                ps.score_run([bad], {})

    def test_invalid_labels_are_rejected_instead_of_rounded(self):
        for label in (True, False, 0.0, 1.0, 0.5, -1, 2, "0", "yes", "canceled", "", [], {}):
            with self.subTest(label=repr(label)), self.assertRaises(ValueError):
                ps.score_run([record()], {"q1": label})
        for labels in (None, [], [("q1", 1)]):
            with self.assertRaises(ValueError):
                ps.score_run([record()], labels)
        with self.assertRaisesRegex(ValueError, "unknown question_id"):
            ps.score_run([record()], {"other-question": 1})
        with self.assertRaisesRegex(ValueError, "nonempty string"):
            ps.score_run([record()], {1: 1})

    def test_invalid_arms_are_rejected_even_when_question_is_unresolved(self):
        for arm in ps.ARMS:
            for value in (None, [], "completed"):
                bad = record(); bad[arm] = value
                with self.assertRaises(ValueError):
                    ps.score_run([bad], {})
            for key, value in (("status", "pending"), ("status", True), ("status", None),
                               ("status", []), ("probability_yes", None),
                               ("probability_yes", True), ("probability_yes", math.nan),
                               ("probability_yes", "0.5"), ("operational_probability_yes", None),
                               ("operational_probability_yes", False),
                               ("operational_probability_yes", 0.2)):
                bad = record(); bad[arm][key] = value
                with self.subTest(arm=arm, key=key, value=repr(value)), self.assertRaises(ValueError):
                    ps.score_run([bad], {})
            for missing in ("status", "probability_yes", "operational_probability_yes"):
                bad = record(); del bad[arm][missing]
                with self.assertRaises(ValueError):
                    ps.score_run([bad], {})

    def test_failed_arm_requires_valid_audit_probability_and_exact_half_fallback(self):
        for probability, operational in ((None, 0.4), (None, "0.5"), (True, 0.5),
                                          ("0.5", 0.5), (math.inf, 0.5), (2, 0.5)):
            bad = record(baseline=probability, baseline_status="failed")
            bad["baseline"]["operational_probability_yes"] = operational
            with self.subTest(probability=probability, operational=operational), \
                    self.assertRaises(ValueError):
                ps.score_run([bad], {})

    def test_small_pilot_contains_no_inference_or_calibration_claim(self):
        for count in (3, 4, 5):
            result = ps.score_run([record(str(i)) for i in range(count)],
                                  {str(i): i % 2 for i in range(count)})
            self.assertIn("cannot establish", result["interpretation"])
            self.assertIn("calibration", result["interpretation"])
            self.assertIn("statistical significance", result["interpretation"])
            self.assertFalse(result["probability_clipping"])


class JsonSafeTests(unittest.TestCase):
    def test_nested_infinities_convert_without_mutating_input(self):
        source = {"a": [math.inf, -math.inf, None, 0.2], "b": (math.inf,)}
        result = ps.to_json_safe(source)
        self.assertEqual(result, {"a": ["Infinity", "-Infinity", None, 0.2], "b": ["Infinity"]})
        self.assertEqual(source["a"][0], math.inf)
        json.dumps(result, allow_nan=False)

    def test_nan_is_rejected(self):
        for value in (math.nan, [math.nan], {"score": math.nan}):
            with self.assertRaisesRegex(ValueError, "NaN"):
                ps.to_json_safe(value)


if __name__ == "__main__":
    unittest.main()
