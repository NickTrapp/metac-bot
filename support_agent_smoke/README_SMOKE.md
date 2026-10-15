# Public G01/G02 smoke execution

This is an explicit execution adaptation of frozen Support Agent Lab v0.2
source commit `672fc801df52d85fc6e681601b737ceae5ac9fd5`: reviewed admission safety
fixes plus a no-install Python standard-library HTTPS transport. The candidate
prompts, public cases, pair order, model snapshot and limits are unchanged.
No private test cases, fixture generator, or grader are included. This smoke is
an integration check, not the full held-out comparison or a quality score.

Run `python3 -m unittest discover -s tests -v` for focused offline tests and
`python3 public_smoke.py` for four public fake-backend episodes. Actual offline
candidate code executes under the original Linux libseccomp boundary. Live
execution explicitly trusts the reviewed Python transport and remote-model
boundary; it is not a network-enabled seccomp sandbox.

## Live execution is separately gated

`python3 public_smoke.py --execute` is blocked unless all source hashes match,
the checkout is the first run/first attempt on the exact dedicated branch, and
the latest commit adds only the designated admission-trigger file. That file
must pin the prepared parent commit, manifest digest, run ID, reviewed pricing
timestamp, short expiry, and an opaque digest of the shared reservation receipt.
There is no trigger file in this preparation. The shared spending ledger stays
private and is not copied here. Existing keys remain scoped to the single live
workflow step and are never copied to this source or an output file.

The run ID is `sal-20261015-v02`; this public stage consumes at most $0.25 from
its existing shared $5 reservation. Each of four one-turn episodes receives an
equal $0.0625 cap. The pinned `gpt-5.4-mini-2026-03-17` configuration uses xhigh,
16,000 maximum input tokens, 8,192 maximum output tokens, and no hosted tools.
Standard prices of $0.75 input / $0.075 cached input / $4.50 output per million
tokens imply a maximum generation envelope of $0.048864 per call, or $0.195456
for this four-call smoke. Actual API token-count preflights are recorded
separately from generation attempts. Official prices must be rechecked before
the trigger is admitted.

The backend sends the same preflight and generation payload fields as the
original SDK adapter. It uses only `api.openai.com` over verified TLS, does not
use proxy environment variables, and never redirects or retries. It projects
function calls and usage fields only; reasoning text and raw error bodies are
not logged. No SDK or other package installation is required.

An exclusive run-state claim and a durable per-episode reservation precede
network work. Unknown usage, model-identity mismatch, provider-contract errors,
worker failures, or accounting overruns stop the smoke before another episode.
Unresolved liability is retained. Invalid model-selected actions are scored
failures, and do not silently disappear from the denominator. The first-run
history gate prevents rerunning a failed or interrupted workflow.

Local reservations are admission controls, not a provider-enforced invoice cap.
The owner of the shared ledger must reconcile final or ambiguous usage before
releasing reservation. This code does not grant separate spending allocations
to other workers or execute the private held-out stage.

Reference: https://developers.openai.com/api/docs/models/gpt-5.4-mini
