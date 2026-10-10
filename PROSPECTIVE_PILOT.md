# Frozen public prospective pilot, 2026-10-10

This is a small prospective **operational and descriptive** comparison, not a
calibration or superiority claim. Five outcomes cannot establish forecast skill,
statistical significance, or generalization. Nothing trades, discovers additional
questions, reads a Metaculus feed, or submits forecasts anywhere.

## Frozen selection

`prospective-manifest-20261010.json` contains five public binary Manifold questions,
selected before any forecasts in deterministic close-time/ID order. The separate
`prospective-selection-audit-20261010.json` records exclusions and independent
primary-source schedule checks. All questions concern distinct October 11 matches
with no shared teams; this does not establish statistical independence.

The runner pins the exact manifest bytes to SHA-256
`a4ad0deeacd8069ec2d3f615aa4a917e4d1418a62ee1ad6014d55ae132aba29d`.
Its normalized, allowlisted manifest and question hashes are additional hashes,
not substitutes for that frozen-file hash. Unknown structured market fields,
including probabilities, volume, liquidity, bets, answers, and price histories,
are never sent to the models. Market URLs and IDs remain record provenance only.
The model sees standalone wording, rules, event start, and primary result URL.
Human review is still necessary to remove odds or forecasts embedded in prose.

The five questions are Everton at Hull, Manchester City at Liverpool, and total
points in Bears–Packers, Vikings–Saints, and Raiders–Patriots. The earliest event
starts October 11 at 13:00 UTC. Expected outcome times are planning estimates,
not guaranteed completion or market resolution times. Original postponement,
cancellation, overtime, draw, and N/A rules remain frozen.

## Comparison fixed before execution

For each question, sequentially:

1. Tool-free baseline forecast, without research
2. One research response, at most two web searches restricted to the frozen
   primary-source domains; every returned citation/source host is checked
3. Tool-free forecast using that research

Both forecasts use the **same exact forecast instructions**, JSON output schema,
pinned `gpt-4.1-mini-2025-04-14`, temperature 0.2, 3,000-output-token limit,
question text, and pair-start as-of timestamp. Only supplied research differs.
Baseline `source_urls` must be empty; researched output must cite supplied
research. Requests are stateless (`store=false`, no conversation/previous-response
IDs), with zero SDK retries, repairs, provider fallback, or follow-up calls.
The historical Apollo prompt/behavior and all prior workflows remain unchanged.

The original research instructions prohibit market/community probabilities.
Primary-domain filtering further reduces exposure; it cannot prove that an
otherwise legitimate page contains no quoted odds or leaked result. Keep this
limitation in any eventual interpretation. Do not inspect outputs and then tune,
replace a failed question, alter criteria, change prompts, or rerun this pilot.

## Timing, failure retention, and budget

Every event must be more than six hours away at initial validation and each pair
start. Both arms must finish within ten minutes of pair start **and still more
than six hours before event start** for a valid pair. Per-call timeout is at most
90 seconds and shrinks to the remaining pair/lead deadline. Time is checked again
after durable checkpoint writes, just before sending. Starts/completions and
elapsed time are recorded; invalid pairs stay in the denominator.

The entire run allows at most 15 reserved outbound attempts and 10 reserved
search calls. Every research request reserves two searches even if fewer are
observed. Unknown/failed attempts never refund the allowance. A checkpoint
records all five questions initially, then each attempt before sending and each
completion. A killed run therefore preserves already-written failures and
unattempted questions at an operational probability of 0.5. Unobserved requests
are explicitly incomplete cost/usage data. If a write fails, execution stops.

A one-time USD 2.50 reservation is retained even on failure. It is a conservative
allowance, **not a provider-enforced billing ceiling**, and must be reconciled
with prior campaign spending before authorizing the trigger. Estimated costs use
the existing reviewed prices; cached input is charged at uncached rates and
search blocks are conservatively added even if already included in returned
usage. Missing usage is unknown, never reported as a complete zero-cost run.
Pricing review expires on October 18. Records preserve model, usage, costs,
source provenance, request/prompt/protocol/question hashes, manifest hashes,
source/trigger commits, run identity, and partial failures.

## Offline review

```sh
python -m unittest discover -s tests -v
python -m py_compile prospective_forecast.py prospective_pilot.py prospective_score.py
python prospective_pilot.py --validate-manifest
```

All normal tests use synthetic questions and fake clients, not the selected
questions or provider calls. The extra `tests/check_prospective_sdk_offline.py`
uses the existing locked OpenAI 2.13.0 and httpx MockTransport with sockets
forbidden; run it under the existing Poetry lockfile in CI before paid execution.
The real SDK contract is distinct from mocked unit tests. Its primary-domain
filter is supported by the official
[2.13.0 web-search schema](https://github.com/openai/openai-python/blob/v2.13.0/src/openai/types/responses/web_search_tool_param.py).

## Guarded publication and execution

1. Review and publish only code, tests, this protocol, the frozen manifest and
   selection audit, and the new workflow. Do **not** include the trigger yet
2. Verify the published source commit and frozen file hash, all offline tests,
   prior spend, and remaining event lead. Do not rename/copy the workflow to reset
   run numbering or delete history
3. Separately add only `.github/prospective-pilot-20261010.trigger`, with exactly
   `public-short-horizon-20261010-v1` and a final newline
4. The sole push-only workflow checks repository, branch, exact clean checkout,
   trigger-only change, frozen manifest, run number 1, attempt 1, and full workflow
   history. It installs the existing lockfile in GitHub Actions, tests the real
   locked SDK offline, then checks the paid gate again before accessing the key
5. The job gets only the existing OpenAI key, never a Metaculus token. It writes
   public-only forecasts/provenance to `prospective-pilot-result.json`, uploaded
   even on failure with 30-day retention. Download and preserve the artifact
   before expiry; the workflow cannot re-run to regenerate it

A same-workspace exclusive claim also blocks a second invocation inside the
sole run. A failed or uncertain execution is a retained observation, not
permission to retry. No trigger is included in this source change.

## Later labels and offline scoring

After official results, create a **separate** label sidecar, without modifying the
frozen questions or forecast record. For each question retain primary source
URL, UTC retrieval timestamp, official final result/status, interpretation under
the frozen rule, and label (integer 0/1 only when unambiguous). Store any source
snapshot/hash needed to audit the result. A market's fractional/probabilistic
resolution is not a binary label. Postponements, missing finals, cancellations,
and disputed date-only boundaries do not become No by default.

The scorer accepts a mapping of question ID to integer 0 or 1; missing/None or
`unresolved` means pending, `ambiguous` means disputed, and `cancelled` stays
unscored. Fractional labels, strings such as `YES`, booleans, and duplicate IDs
are rejected rather than coerced. Keep checking a postponed outcome only under
its original rule; never invent a timezone for an unspecified date boundary.

```python
import json
from prospective_score import score_run, to_json_safe
run = json.load(open("prospective-pilot-result.json"))
# labels comes from independently verified label-sidecar outcomes, not model text.
labels = {}
report = score_run(run["records"], labels)
print(json.dumps(to_json_safe(report), indent=2, allow_nan=False))
```

The report separates:

- Successful pairs: both arms completed, valid timing, unambiguous binary label
- Operational results: every resolved retained question, failed arms at p=0.5;
  this includes invalid-timing pairs and is not a leakage-free skill estimate

Report per-question binary Brier `(p-y)^2`, natural-log loss, both arm means,
and paired researched-minus-baseline differences (negative favors research).
A wrong p=0 or p=1 gives real infinity, never epsilon clipping. Strict JSON
serializes it as `"Infinity"`/`"-Infinity"`; infinity minus infinity is undefined
(`null` plus an explicit reason), never silently zero. Always report counts,
failures, unscored outcomes, and all exclusion reasons beside the descriptive
comparison. No model changes should be proposed from these five outputs alone.
