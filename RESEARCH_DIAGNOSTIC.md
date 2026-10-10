# One resolved Apollo research diagnostic

This is a separate operational debugging test. It is not a new prospective
forecast, a rerun of the ended pilot, or evidence of forecast accuracy.

## Fixed scope

- Exactly one OpenAI Responses request, with no forecast arm or live questions
- Reuse the existing resolved Apollo fixture and the actual
  `prospective_forecast.research_request` / `question_input` builder
- Same model, prompt, request limits, stateless settings, and `nasa.gov` filter
- At most two search tool calls, 3,000 output tokens, 90-second timeout
- Zero SDK retries, parser retries, fallback requests, or manual reruns
- Existing GitHub Actions OpenAI secret only; no Metaculus credentials or writes
- Existing $0.25 conservative reservation for this diagnostic; estimated normal
  cost is a few cents, and the reservation is not a provider billing ceiling
- Existing pricing-review deadline remains October 18, 2026 UTC

This adds a new workflow and trigger namespace. All old workflows, old trigger
gates, the frozen manifest, and original result artifacts remain untouched.
The separate diagnostics patch changes only safe metadata in the prospective
handler; its requests, protocol and frozen cohort are unchanged.

## Retained output

Only fixed operational metadata, timestamps, commit/run provenance, request
hash, allowlisted response ID/token counts, measured usage/cost estimate,
citation count, source-validation status, and safe failure diagnostics are
written to `research-diagnostic-result.json`. No response text, search query,
source URL, exception message/body, request headers, or credentials are saved.

Failures distinguish SDK request, response metadata, response validation, and
source validation. Exception class, HTTP status, and provider code use the
closed vocabulary described in `DIAGNOSTICS_FIX.md`. Unknown values stay null.
Cost stays null if accepted usage is unavailable; a missing value is not zero.
The original pilot's discarded errors and unknown costs cannot be recovered
by this test, even if it reveals a reproducible failure with the same builder.

## First-run gates

Publish and review source before adding the single fixed marker:

` .github/research-diagnostic-20261010.trigger ` with contents
`apollo-primary-domain-research-diagnostic-v1` plus a trailing newline.

The trigger commit must add only that file on main. Checkout must be clean.
The job and runner require the new workflow's sole run number 1 and attempt 1,
and validate the complete one-entry history through read-only Actions access.
An exclusive local claim prevents duplicate execution in the same workspace.
Claim failures preserve the previous artifact. No dispatch, schedule, reset,
or retry is provided. Publishing the source alone sends no paid request.

## Verification

Portable offline tests:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

The new workflow uses the existing approved Poetry installation setup and
unchanged lockfile. Before accessing the OpenAI secret, it runs:

```sh
poetry run python tests/check_research_diagnostic_sdk_offline.py
```

That check requires the exact locked SDK version, blocks all network sockets,
and uses an in-memory MockTransport. It checks exact request serialization,
successful parsing, actual SDK 400/429 exceptions, zero retries, and unsafe
message/code suppression. MockTransport success does not prove that the live
provider supports a request. No packages are installed in the local workspace.

If the locked SDK preflight or sole live attempt fails, preserve the evidence
and stop. Any future different paid test needs its own reviewed scope; failure
never unlocks another attempt of this diagnostic or the frozen pilot.
