# Initial setup: safe OpenAI connectivity test

This fork is intentionally paused. There are **no scheduled runs**, and the
legacy main runners exit before doing work. The original smoke test submits no
Metaculus prediction or comment. A separately approved, one-shot testing-area
submission is documented in [END_TO_END_TEST.md](END_TO_END_TEST.md).
Do not follow the upstream live-run instructions
in README until a separate live configuration has been reviewed and authorized.

## What is ready

- `Test Bot (offline)` is a repeatable, manual, standard-library safety test. It
  needs no secrets, package installs, LLM calls, or Metaculus access
- `Initial OpenAI smoke test` is a manual **one-shot** campaign. It checks one
  synthetic fair-coin question using the official baseline's binary prompt and
  typed-parser path, then prints the probability and non-sensitive usage totals
- Direct OpenAI only: `gpt-4.1-mini-2025-04-14`, temperature 0.2, one completion per
  request, at most 2,048 output tokens, no research, tools, provider fallback,
  SDK retry, or parser retry
- Two request attempts maximum for the entire supported campaign: one forecast
  and one parse. Attempts are counted before sending, including ambiguous
  timeouts and failed responses. Nothing refunds a failed attempt
- The paid workflow gets only `OPENAI_API_KEY`. It has no Metaculus credential.
  The bot has publishing off, no real question IDs, and an unusable Metaculus client
- All official baseline question handlers are retained: binary, multiple choice,
  numeric/discrete, date, and conditional. The paid test exercises binary only

This cheap model checks connectivity and formatting. It is not a claim of
competitive forecasting quality or a completed end-to-end tournament integration.

## Cumulative spending scope

The user's authorization is **USD 10 total across all initial test calls and
retries**, not USD 10 each run. This configuration deliberately uses much less:

- At the verified prices below, even a full 1,047,576-token context plus 2,048
  output tokens costs at most USD 0.4223072 per request
- The guard conservatively reserves USD 0.50 before each attempt, with two attempts
  maximum: USD 1.00 reserved, and USD 0.8446144 maximum modeled token cost
- Successful responses report uncached-rate token cost from returned usage. That
  is not an invoice, and failures keep their entire conservative reservation
- Model and pricing assumptions must be rechecked after 2026-10-17. This runner
  refuses to execute after that date

There is no provider/account-wide financial cap or persistent ledger here.
Cross-run protection is deliberately one-shot: only manual run number 1,
attempt 1, on this repository's main-branch workflow is allowed. GitHub must
return exactly that one run in complete workflow history. Missing history or a
failed history request blocks paid work. All branches share one concurrency group.
Re-runs and later runs are blocked, even if the first failed during installation
before making an API call.

**Do not rename/recreate/copy this workflow or override its checks to get a
second run.** Such changes, other scripts, and unrelated use of the same API key
are outside this protection. Deleted history alone cannot reset run number.
Before any new test, review cumulative spend and obtain authorization for the
revised configuration. Do not treat an ephemeral Actions workspace/artifact as a
durable spending ledger. OpenAI project spend controls can be added as defense
in depth, but are not assumed to be configured.

Sources checked 2026-10-10:
- [OpenAI model, limits, and pricing](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
- [OpenAI spend controls](https://developers.openai.com/api/docs/guides/spend-limits)
- [Original baseline commit](https://github.com/NickTrapp/metac-bot/commit/da5de87fcfefc324af9aa268cbc4efdd6b9fbfea)

## Checks and execution

Run the dependency-free tests locally:

```sh
python -m unittest discover -s tests -v
```

After the existing Poetry lockfile is installed, the dependency-backed offline
check exercises the real baseline/typed parsers with fake SDK responses and a
Python socket connection guard (not a separate network sandbox):

```sh
poetry run python tests/check_baseline_offline.py
```

That check covers binary, multiple choice, numeric, date, malformed binary input,
and conditional composition. It is a prerequisite step in the paid workflow.
The first paid run installs the locked main dependencies, runs both offline
checks, verifies the one-shot gate again, then makes the two permitted requests.
No keys are needed for either offline check. SDK error bodies and upstream
parser logs are suppressed so public logs cannot expose credential fragments.

Running `python safe_smoke_test.py` without `--execute` is offline. The supported
paid entry point is the reviewed GitHub `Initial OpenAI smoke test` workflow,
manually selected once. It is not scheduled. Do not run it until the safe
configuration has been reviewed and paid execution is authorized.

No production/tournament workflow is enabled by a successful test. A later live
configuration needs a separate decision on research, model quality, coverage,
cost controls, schedule, and publication.
