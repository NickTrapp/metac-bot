"""Offline scaffold and a one-shot, non-network credential-presence check.

There is deliberately no paid execution implementation in this preparation.
The presence check never validates or displays a credential's value.
"""
from __future__ import annotations

import argparse
import json
import os

REPOSITORY = "NickTrapp/metac-bot"
BRANCH = "support-agent-experiment-20261015"
WORKFLOW = "support_agent_readiness_20261015.yaml"
EXPERIMENT_ID = "support-agent-20261015-v1"


class ReadinessBlocked(Exception):
    """A fixed, non-sensitive failure reason."""


def offline_plan() -> dict:
    return {
        "experiment_id": EXPERIMENT_ID,
        "status": "awaiting_frozen_experiment_and_shared_budget_admission",
        "paid_execution_enabled": False,
        "paid_requests": 0,
        "new_reservation_usd": "0.00",
        "maximum_experiment_allocation_usd": "5.00",
        "reservation_is_provider_billing_cap": False,
    }


def verify_presence_context(env) -> None:
    expected = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/" + BRANCH,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_RUN_NUMBER": "1",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/{BRANCH}",
    }
    if any(env.get(key) != value for key, value in expected.items()):
        raise ReadinessBlocked("credential_presence_context_blocked")


def credential_presence(env) -> dict:
    verify_presence_context(env)
    # This value is consumed only by bool(), never included in any output.
    return {"openai_api_key_present": bool(env.get("OPENAI_API_KEY"))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--credential-presence", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.execute:
        print(json.dumps({"error": "paid_execution_not_enabled", "paid_requests": 0}))
        return 2
    try:
        result = credential_presence(os.environ) if args.credential_presence else offline_plan()
    except ReadinessBlocked:
        print(json.dumps({"error": "credential_presence_context_blocked"}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
