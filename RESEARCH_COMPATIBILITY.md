# Pinned-model research compatibility, version 1

The controlled resolved-Apollo pair returned HTTP 400 with parameter `tools`
when the request contained `tools[0].filters`. Removing only that filter allowed
the otherwise identical request to complete with valid NASA citations. Evidence:
[control pair, run 38072988473](https://github.com/NickTrapp/metac-bot/actions/runs/38072988473).

`research_compatibility_v1.py` makes this one explicit compatibility change for
`gpt-4.1-mini-2025-04-14`: it removes the request-side domain filter. It leaves
the research prompt, model, stateless settings, search/output/request limits,
and source-domain validation unchanged. The helper rejects other models instead
of silently extending this compatibility claim.

## Important research limitation

Post-hoc citation/source validation cannot guarantee market-blind research.
Unfiltered retrieval can expose prediction-market or community information
before the output is checked. The research prompt still forbids using such
information, but that is not a hard retrieval restriction. Records explicitly
state `market_blind_research: false`.

This method is separately versioned. It does not change the frozen original
pilot protocol, its five failed research arms, its manifest, or its scoreable
denominator. It must not be silently substituted into that pilot. Any future
prospective study needs a fresh reviewed protocol that addresses this limitation.

## Remaining approved debugging check

The owner approved publication of debugging changes and at most four controlled
fact-check requests under $2.50. The comparison pair used two. This new check
uses at most the remaining two, in this order:

1. Corrected unfiltered research on the same resolved Apollo fixture, validated
   against NASA citation and retrieved-source hosts
2. A tool-free structured factual answer using only that research, with a fixed
   schema: yes/no/unknown, rationale, uncertainties, and cited source URLs

The factual-answer stage is a resolved integration check, not a probability or
forecast. Its citations must have appeared in the research. If research or its
source validation fails, there is no second call. No stage is retried, repaired,
or replaced. There are no forecasts, market posts, or Metaculus submissions.

Each request has a 90-second timeout and 3,000-token output limit. Research allows
at most two searches; the answer has `tools: []` and `tool_choice: none`. The
request/input bound and pinned model remain unchanged. Existing locked Poetry
dependencies and the existing Actions OpenAI secret are used without new access.

The additional reservation is $0.50, making $1.00 across the approved four-call
batch, within $2.50. This is a conservative reservation, not a provider-side
billing cap. Known prior usage was $0.0172996; the rejected request's cost is
unknown. New known usage is added separately; total batch cost stays unknown
until all usage is known. Unknown does not mean zero. A known-cost guard blocks
another request if its $0.25 reservation would exceed the approved amount.

## Evidence retained

The artifact keeps safe phase/class/status/code/parameter diagnostics, request
hashes, bounded response identifiers and token counts, known cost estimates,
timestamps, and source/trigger/run provenance. It also retains public NASA
source URLs when they are HTTPS, have no credentials/port/query/fragment/control
characters, and are at most 2,048 characters long. Unsafe URLs are omitted and
counted. Source validation still occurs before any answer request.

The public answer record contains only yes/no/unknown and safe cited NASA URLs.
Raw research/answer text, rationale, search queries, provider error messages or
bodies, request headers, account details, and credentials are not persisted.

## Gating and validation

A new, separate trigger-only commit adds
`.github/research-compatibility-20261010.trigger` with
`apollo-research-factual-answer-compatibility-v1` and a newline.
Source must be published beforehand. The runner requires a clean main checkout,
exact marker, sole run 1/attempt 1, and an exclusive durable local claim.
It also verifies that the first-stage comparison is exactly its original
completed successful run 38072988473, attempt 1, trigger a64611d099906deeeb05d7a73f4e97df88fb3784.
An additional or changed prior run blocks this check. The combined attempt
counter cannot exceed four; this entry point cannot exceed two.

The workflow runs the full stdlib offline suite and then, with unchanged locked
dependencies, `tests/check_compatibility_sdk_offline.py`. Four MockTransport
tests block all sockets, require the exact locked SDK, verify the corrected
research/strict tool-free answer shapes and actual SDK error handling, and check
that failures do not cause retries. These checks must pass before live execution.
No local installation is required or performed.

Success verifies only the resolved historical research-to-answer path. It does
not establish forecast skill, calibration, or market-blind research quality.
