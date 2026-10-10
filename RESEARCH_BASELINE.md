# Minimal researched binary baseline

`researched_forecast.py` is an original, dependency-light adapter with one
web-research response and one tool-free, structured binary-forecast response.
It does not change the existing bot, submission guards, or workflows. Importing
or running the module does nothing external. No real questions were used to
develop its fixtures.

## Implemented

- Pinned `gpt-4.1-mini-2025-04-14`, Responses API, at most two `web_search` calls
  in the research response, and no tools in the forecast response
- At most 3,000 output tokens per response; the complete submitted JSON request
  is bounded to 32,768 UTF-8 bytes, including instructions and output schema
- Direct endpoint, zero SDK retries, no provider fallback, continuation IDs,
  parser/repair calls, or Metaculus access
- Research prompts ask for resolution interpretation, dated facts, base rates,
  evidence in both directions, and uncertainties; question/source content is
  treated as untrusted evidence
- JSON forecast with a probability in [0, 1], explanation, uncertainties, and
  source URLs checked against the research citations
- Structured record of the question snapshot/hash, UTC run date, response IDs,
  usage, research text, citation spans, source URLs, and estimated costs
- Citation publication dates are explicitly unknown in structured provenance;
  the report is asked to describe source/event dates where available. A run
  timestamp does not establish when a source was published
- Missing search, missing/malformed citations, incomplete output, invalid JSON,
  refusal, or unexpected tools stop the run without a retry

## Cost accounting

The callback supplied as `reserve` must persist a reservation against the
caller's cumulative allowance **before** either request. A failed or ambiguous
request retains it. The integration runner uses its verified sole GitHub run
as the durable one-shot claim. The adapter itself is not a cross-run ledger.
On failure, its exception retains measured usage and completed research from
earlier stages; the public summary distinguishes partial from complete usage.

USD 0.25 is a conservative candidate reservation, **not a proven hard cap**.
Standard model pricing checked 2026-10-10 is USD 0.40/M input tokens and USD
1.60/M output tokens. Each non-preview web-search call costs USD 0.01 plus a
fixed 8,000 input-token block for this model. Two searches and the maximum
6,000 output tokens total USD 0.036 before ordinary input. The nominal
byte-based input estimate adds USD 0.0262144. Server framing and possible
repeated internal input are not fully specified, so submitted byte limits alone
do not prove a billing ceiling.

Returned usage is charged at uncached rates for the estimate; search blocks
are also added, conservatively double-counting if already included in usage.
This is not an invoice. Pricing must be reviewed again on/after 2026-10-18.

## Offline checks

```sh
python -m unittest discover -s tests -v
python -m py_compile researched_forecast.py tests/test_researched_forecast.py
```

Synthetic fixtures cover both request payloads, source/date provenance, valid
forecast parsing, multiple text/citation spans, budget ordering, failures on
either request, missing/malformed evidence, oversized input, refusals/incomplete
responses, unexpected tools, and malformed/out-of-range forecasts. They test
operational behavior, not forecast skill, source truth, or live API acceptance.
The record always labels forecast skill as `not_evaluated`.

`poetry.lock` pins OpenAI SDK 2.13.0. Its official source supports the request
parameters used here. `tool_choice="required"` with only the web tool avoids a
specific-tool-choice compatibility issue in that version. A pre-existing SDK
2.24.0 passed a mocked-transport review, but the exact locked 2.13.0 runtime
was unavailable locally. Its SDK-backed contract check must pass in the
workflow before any paid call. Existing baseline checks remain separate.

## One-shot integration execution

`research_integration_check.py` uses one fixed, resolved historical question:
whether Apollo 11 landed on the Moon on July 20, 1969 UTC. A completed result
must have a NASA citation and probability greater than 0.5 for the known-Yes
answer. This is a semantic integration sanity check, not forecasting skill.
It never retries an unexpected answer.

Publication is deliberately two commits:

1. Publish reviewed code, tests and workflow without the trigger file
2. Add only `.github/research-integration-20261010.trigger`, containing
   `apollo-11-resolved-fact-operational-check-v1` and a final newline

The push-only workflow installs the existing lockfile, runs the offline tests
and a real-SDK/MockTransport contract check, then checks exact checkout SHA,
trigger-only addition, repository, main branch, run number 1, attempt 1, and
complete workflow history before calling OpenAI. It reuses existing OpenAI
credentials and never receives a Metaculus token. Re-runs, additional pushes,
changed markers, or combined code/trigger commits cannot enable another test.
Do not rename/copy this workflow to reset it. No schedule is configured.

The adapter returns the record to its caller and does not save, print, upload,
or publish it. Keep question text, research, probabilities, and explanations
out of public Actions logs/artifacts unless explicitly approved for that
specific synthetic/public demonstration. This test retains only the public
Apollo fixture result/provenance and cost data in a seven-day artifact; it
prints concise operational/cost counters, not research or provider errors.
Additional Metaculus submissions, scored questions,
scheduled execution, and claims of measured forecasting skill are separate
decisions.

## References checked 2026-10-10

- [Web search](https://developers.openai.com/api/docs/guides/tools-web-search)
- [Pricing](https://developers.openai.com/api/docs/pricing)
- [Model](https://developers.openai.com/api/docs/models/gpt-4.1-mini)
- [Responses parameters](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
- [SDK 2.13.0 tool schema](https://github.com/openai/openai-python/blob/v2.13.0/src/openai/types/responses/web_search_tool_param.py)
- [SDK 2.13.0 request parameters](https://github.com/openai/openai-python/blob/v2.13.0/src/openai/types/responses/response_create_params.py)
- [SDK 2.13.0 tool choice](https://github.com/openai/openai-python/blob/v2.13.0/src/openai/types/responses/tool_choice_types_param.py)
