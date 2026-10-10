# Controlled Apollo domain-filter comparison

This separately authorized operational experiment makes two research requests:
one with the `nasa.gov` domain filter, then one without that filter. It uses the
same resolved Apollo fixture and pinned model as the prior one-request check.
It is not a prospective forecast, a new forecasting cohort, or a repair/retry
of the original five-question pilot. Original results remain unchanged.

## Controlled difference and stopping rule

The runner builds the filtered payload with the actual prospective research
builder, deep-copies it, and removes only `tools[0].filters`. The model, prompt,
input text, frozen `as_of_utc`, historical event, source preference, tool choice,
limits, and all other request fields are identical. Per-request hashes are kept.

The first-stage pair uses two of the owner's approved maximum four requests.
It stops after those two attempts, even if inconclusive. There are no retries,
fallbacks, repeated controls, new prompts, or additional paid requests. A
different informative control would require a separate reviewed implementation.
The original pilot and earlier diagnostic workflows cannot be rerun through
this entry point.

## Budget and transport

- One fixed pinned-model request per variant; two total attempts
- At most two searches per request and 3,000 output tokens
- Direct existing OpenAI endpoint, zero SDK retries, 90-second request timeout
- Existing OpenAI Actions secret only; no Metaculus secret or submissions
- $0.50 conservative combined reservation within the approved $2.50 maximum
- Expected total usage is a few cents; the reservation is not a provider-side
  billing cap. The bounded fixture and request/search/output limits control
  spend. A known-cost guard also blocks the next request if another $0.25
  reservation would exceed $2.50
- Cost stays unknown where usage is absent. The sum of known usage is labeled
  separately, never substituted for the total when any usage is missing
- Existing October 18, 2026 pricing-review deadline remains in force

## Safe diagnostics and interpretation

The public artifact contains only fixed test metadata, commit/run provenance,
timestamps, request hashes, bounded response IDs and integer token counts,
usage/cost estimates, citation counts, source-policy status, and closed-vocabulary
error metadata. It never saves model text, query text, source URLs, exception
messages, error bodies, headers, account details, or credentials.

`provider_param` reads only the SDK exception's parsed `param` attribute. It
accepts exact strings from a fixed 26-name allowlist of fields actually present
in the research payload, including only index zero for its single tool/domain/
include entry. Unknown paths, string subclasses, objects, and failing getters
produce null. No arbitrary regular expression or free-text extraction is used.

Both responses undergo identical parsing and NASA source-policy validation.
`sdk_returned` records API return separately: a response that later fails source
validation still demonstrates API acceptance. A filtered HTTP 400 followed by an
unfiltered response isolates the filter-setting change as the difference
associated with rejection in this pair; `provider_param` may identify the
rejected field more precisely. Other patterns may remain inconclusive.
This test cannot recover the original five discarded exceptions or establish
that their exact cause was identical.

## One-shot execution

Source is reviewed and published first. A separate commit then adds only
`.github/research-filter-comparison-20261010.trigger`, containing
`apollo-filter-vs-unfiltered-comparison-v1` and a newline.

The new workflow requires main, the sole run number 1 and attempt 1, a clean
checkout, exact trigger-only commit, and complete matching one-run Actions
history. An exclusive local claim prevents duplicate same-workspace execution.
Every attempt is checkpointed before sending and afterward. Claim failure
preserves an existing artifact. There is no dispatch, schedule, or reset path.

Before using the existing secret, the workflow runs the portable offline suite,
installs unchanged locked dependencies through the existing setup, then runs
`tests/check_filter_comparison_sdk_offline.py`. That check blocks all sockets,
requires the exact locked SDK, and uses MockTransport to verify both payloads,
actual SDK error.param extraction, unknown-field suppression, and no retries.
SDK serialization success alone does not establish live provider compatibility.

A successful workflow means the controlled experiment completed both attempts;
it does not mean both provider requests or source validations succeeded. Read
each attempt and `comparison_outcome` in the artifact for the actual result.
