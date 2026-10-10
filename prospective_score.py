"""Offline, stdlib-only scoring of retained binary prospective forecasts.

The successful-pair analysis requires valid timing and two completed arms.
The operational analysis includes *every resolved retained question*, including
invalid-timing pairs, and assigns a failed arm the preregistered 0.5 fallback.
It therefore describes operational outcomes, not a leakage-free skill estimate.
Unresolved, cancelled, and ambiguous outcomes are retained but never scored.

Probabilities must be finite JSON numbers in [0, 1]; binary outcomes must be
actual integers 0 or 1. Booleans, strings, and fractional labels are not coerced.
Log loss uses natural logarithms without epsilon clipping. A confidently wrong
endpoint has float('inf') loss. Undefined infinity-minus-infinity differences
are None with a reason. Use to_json_safe before strict JSON serialization.

Importing this module performs no file, credential, network, or provider access.
"""

from collections.abc import Mapping
import math


ARMS = ("baseline", "researched")
NON_BINARY_STATUSES = ("unresolved", "cancelled", "ambiguous")
SMALL_PILOT_CAUTION = (
    "Descriptive pilot results only. A 3–5-question pilot cannot establish "
    "forecasting skill, calibration, statistical significance, or generalization. "
    "Negative researched-minus-baseline differences favor researched forecasts. "
    "Operational scores include failed-arm 0.5 fallbacks and invalid-timing pairs; "
    "they are not a leakage-free forecast-skill comparison."
)


def _probability(value, name="probability_yes"):
    # Exact types deliberately exclude bool and non-JSON numeric objects.
    if type(value) not in (int, float) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a finite number in [0, 1], excluding bool")
    return float(value)


def _binary_label(value):
    if type(value) is not int or value not in (0, 1):
        raise ValueError("binary label must be int 0 or 1, excluding bool")
    return value


def brier_score(probability_yes, label):
    """Return the binary Brier score (p - y)**2, on the [0, 1] scale."""
    probability = _probability(probability_yes)
    return (probability - _binary_label(label)) ** 2


def log_loss(probability_yes, label):
    """Return binary negative natural log likelihood, preserving infinity."""
    probability = _probability(probability_yes)
    outcome = _binary_label(label)
    if probability == outcome:
        return 0.0
    if probability in (0.0, 1.0):
        return math.inf
    return -math.log(probability) if outcome == 1 else -math.log1p(-probability)


def _scores(probability, label):
    return {"brier": brier_score(probability, label),
            "log_loss": log_loss(probability, label)}


def _difference(researched, baseline, *, empty=False):
    if empty:
        return {"brier": None, "log_loss": None,
                "log_loss_undefined_reason": "no_scored_pairs"}
    both_infinite = math.isinf(researched["log_loss"]) and math.isinf(baseline["log_loss"])
    return {
        "brier": researched["brier"] - baseline["brier"],
        "log_loss": None if both_infinite else researched["log_loss"] - baseline["log_loss"],
        "log_loss_undefined_reason": "both_log_losses_infinite" if both_infinite else None,
    }


def _mean(values):
    if not values:
        return None
    # Score inputs are nonnegative: no opposite infinities or NaN can occur.
    return math.inf if any(math.isinf(value) for value in values) else math.fsum(values) / len(values)


def _summarize(rows, inclusion_key, score_key):
    included = [row for row in rows if row[inclusion_key]]
    arm_scores = {
        arm: {metric: _mean([row[arm][score_key][metric] for row in included])
              for metric in ("brier", "log_loss")}
        for arm in ARMS
    }
    delta = _difference(arm_scores["researched"], arm_scores["baseline"], empty=not included)
    if delta["log_loss_undefined_reason"] == "both_log_losses_infinite":
        delta["log_loss_undefined_reason"] = "both_arm_mean_log_losses_infinite"
    return {
        "count": len(included),
        "question_ids": [row["question_id"] for row in included],
        "baseline": arm_scores["baseline"],
        "researched": arm_scores["researched"],
        "researched_minus_baseline": delta,
    }


def _validate_arm(value, path):
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    status = value.get("status")
    if type(status) is not str or status not in ("completed", "failed"):
        raise ValueError(f"{path}.status must be 'completed' or 'failed'")
    if "probability_yes" not in value:
        raise ValueError(f"{path}.probability_yes is required")
    probability = value["probability_yes"]
    if probability is not None or status == "completed":
        probability = _probability(probability, f"{path}.probability_yes")
    operational = _probability(value.get("operational_probability_yes"),
                               f"{path}.operational_probability_yes")
    expected = probability if status == "completed" else 0.5
    if operational != expected:
        raise ValueError(f"{path}.operational_probability_yes must equal "
                         "the completed probability or 0.5 on failure")
    return {"status": status, "probability_yes": probability,
            "operational_probability_yes": operational}


def _question_id(value, path):
    if type(value) is not str or not value.strip():
        raise ValueError(f"{path} must be a nonempty string")
    return value


def _validate_labels(labels, question_ids):
    if not isinstance(labels, Mapping):
        raise ValueError("labels must be a mapping from question_id to outcome")
    for question_id, label in labels.items():
        _question_id(question_id, "label question_id")
        if question_id not in question_ids:
            raise ValueError(f"label for unknown question_id: {question_id}")
        if label is None:
            continue
        if type(label) is str and label in NON_BINARY_STATUSES:
            continue
        _binary_label(label)


def _failure_counts(rows):
    return {
        "baseline": sum(row["baseline"]["status"] == "failed" for row in rows),
        "researched": sum(row["researched"]["status"] == "failed" for row in rows),
        "either_arm": sum(any(row[arm]["status"] == "failed" for arm in ARMS) for row in rows),
        "both_arms": sum(all(row[arm]["status"] == "failed" for arm in ARMS) for row in rows),
    }


def score_run(records, labels):
    """Score a list of retained question records against a partial label mapping.

    Required record fields are question_id (unique nonempty str), pair_valid
    (bool), and baseline/researched mappings with status, probability_yes, and
    operational_probability_yes. Completed operational probabilities must equal
    their forecast; failed operational probabilities must equal 0.5. A failed
    arm may retain a valid probability for audit, but it is not scored as a
    completed forecast. All records are validated even when unresolved.

    Labels may be int 0/1, None, 'unresolved', 'cancelled', or 'ambiguous'. Missing
    labels count as unresolved. Fractional labels and unknown question IDs raise
    ValueError. Inputs are not modified. Result means use the same questions for
    both arms and return researched-minus-baseline differences; negative favors
    researched. Per-question differences are retained for both analysis sets.
    Invalid timing excludes a successful pair even if both arms completed.
    """
    if type(records) is not list:
        raise ValueError("records must be a list")
    validated = []
    seen = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ValueError(f"records[{index}] must be a mapping")
        question_id = _question_id(record.get("question_id"), f"records[{index}].question_id")
        if question_id in seen:
            raise ValueError(f"duplicate question_id: {question_id}")
        seen.add(question_id)
        if type(record.get("pair_valid")) is not bool:
            raise ValueError(f"{question_id}.pair_valid must be bool")
        validated.append({"question_id": question_id, "pair_valid": record["pair_valid"],
                          **{arm: _validate_arm(record.get(arm), f"{question_id}.{arm}")
                             for arm in ARMS}})
    _validate_labels(labels, seen)

    rows = []
    for record in validated:
        question_id = record["question_id"]
        label = labels.get(question_id)
        resolved = type(label) is int
        outcome_status = "resolved" if resolved else (label or "unresolved")
        completed = all(record[arm]["status"] == "completed" for arm in ARMS)
        successful = resolved and record["pair_valid"] and completed
        exclusions = []
        if not resolved:
            exclusions.append(outcome_status)
        if not record["pair_valid"]:
            exclusions.append("invalid_timing_pair")
        exclusions.extend(f"{arm}_failed" for arm in ARMS if record[arm]["status"] == "failed")
        row = {"question_id": question_id, "label": label,
               "label_provided": question_id in labels, "outcome_status": outcome_status,
               "pair_valid": record["pair_valid"],
               "successful_pair_included": successful,
               "operational_included": resolved,
               "successful_pair_exclusion_reasons": exclusions}
        for arm in ARMS:
            data = record[arm]
            row[arm] = {
                **data,
                "forecast_scores": _scores(data["probability_yes"], label)
                if resolved and data["status"] == "completed" else None,
                "operational_scores": _scores(data["operational_probability_yes"], label)
                if resolved else None,
            }
        row["successful_pair_difference"] = _difference(
            row["researched"]["forecast_scores"], row["baseline"]["forecast_scores"]
        ) if successful else None
        row["operational_difference"] = _difference(
            row["researched"]["operational_scores"], row["baseline"]["operational_scores"]
        ) if resolved else None
        rows.append(row)

    resolved_rows = [row for row in rows if row["operational_included"]]
    unresolved_ids = [row["question_id"] for row in rows if not row["operational_included"]]
    invalid_timing_ids = [row["question_id"] for row in rows if not row["pair_valid"]]
    successful_pairs = _summarize(rows, "successful_pair_included", "forecast_scores")
    operational = _summarize(rows, "operational_included", "operational_scores")
    return {
        "schema_version": "prospective_binary_scores_v1",
        "difference_direction": "researched_minus_baseline",
        "logarithm": "natural",
        "probability_clipping": False,
        "failed_arm_fallback_probability": 0.5,
        "interpretation": SMALL_PILOT_CAUTION,
        "counts": {
            "retained": len(rows), "resolved": len(resolved_rows),
            "unresolved_or_excluded_outcomes": len(unresolved_ids),
            "missing_labels": sum(not row["label_provided"] for row in rows),
            **{status: sum(row["outcome_status"] == status for row in rows)
               for status in NON_BINARY_STATUSES},
            "invalid_timing_pairs": len(invalid_timing_ids),
            "resolved_invalid_timing_pairs": sum(not row["pair_valid"] for row in resolved_rows),
            "successful_pairs": successful_pairs["count"],
            "operational_pairs": operational["count"],
        },
        "failure_counts": _failure_counts(rows),
        "resolved_failure_counts": _failure_counts(resolved_rows),
        "unresolved_ids": unresolved_ids,
        "unresolved_ids_by_status": {
            status: [row["question_id"] for row in rows if row["outcome_status"] == status]
            for status in NON_BINARY_STATUSES
        },
        "invalid_timing_ids": invalid_timing_ids,
        "excluded_successful_pair_ids": [row["question_id"] for row in rows
                                         if not row["successful_pair_included"]],
        "successful_pairs": successful_pairs,
        "operational": operational,
        "per_question": rows,
    }


def to_json_safe(value):
    """Return a JSON-compatible copy, spelling infinities as strings.

    NaN is rejected rather than silently written or converted. This preserves
    undefined differences as None plus their explanatory reason in score_run.
    Serialize the result using json.dumps(..., allow_nan=False).
    """
    if isinstance(value, float):
        if math.isnan(value):
            raise ValueError("NaN is not a valid score or JSON value")
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, Mapping):
        return {key: to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]
    return value
