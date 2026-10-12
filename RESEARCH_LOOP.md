# Research loop starter

The first goal is one complete research-assisted result with a comparable
baseline, retained evidence, and measurable resource use. The new runner lives
alongside the frozen October 10 pilot. It does not reinterpret that pilot's
failed research attempts as completed forecasts.

## Start here

`python research_loop.py` validates the committed development fixture and prints
an offline plan. It does not read API credentials or send requests.

After merging this change, open **Actions → Research loop (manual) → Run
workflow**. Keep `execute` unchecked for the first offline initialization. To
send the development requests, run the workflow again with `execute` checked
and keep `budget_usd` at `1.00`. It uses the existing `OPENAI_API_KEY` repository
secret. No Metaculus token is needed.

The fixture asks a known historical Apollo 11 question. A successful run has
three requests, one complete baseline/researched pair, valid citation
provenance, and recorded token/search usage. It is an integration check and
cannot establish forecasting skill.

For local execution, install the existing OpenAI SDK and set `OPENAI_API_KEY`
outside the repository, then run:

```bash
python research_loop.py --initialize
python research_loop.py --execute
```

Rerun the same command against the same ledger to continue pending stages.
Completed stages are reused. Failed or ambiguous requests stop for review and
are never automatically repeated. The default ledger is
`.work/research-loop.sqlite`; the report is `.work/research-loop-report.json`.
Keep the ledger when moving machines. Deleting it would lose attempt and
allowance history.

## What the loop does

1. Freeze the task wording, resolution rule, primary domains, and protocol.
2. Record a Sol baseline without web research.
3. Gather dated evidence with Luna and primary-domain web search.
4. Ask Sol for the researched forecast using the same prompt, reasoning effort,
   and output-token allowance as the baseline.
5. Check JSON, probability range, citation spans, primary hosts, and exact cited
   URLs; save the result or a fixed failure code.

The forecast schema restricts cited URLs to the actual research citations.
Independent parsing checks them again. Public URLs with tracking query strings
are retained, preserving the provenance previously lost by a display filter.
The new response reader accepts reasoning items without exporting them.
These are structural provenance checks, not proof that every cited claim is
entailed by its source. Domain restrictions also do not prove complete absence
of prediction-market knowledge.

## Limits and persistence

The starter is serial: one task at a time, at most twenty frozen questions per
manifest, three requests per question, two research tool calls, and 8,192 output
tokens per request. The SDK performs zero automatic retries. Requests are
stateless; research is supplied explicitly to the final forecast.

SQLite commits each attempt and its $0.25 reservation **before** sending it.
The default $1 allowance covers the fixture's $0.75 total reservation. The
entire campaign must fit its allowance before any request is admitted. Failed
or ambiguous calls retain their reservations; unknown billing is reported as
unknown. Measured estimates above a reservation consume the larger amount.
This is an application reservation limit, not a provider-enforced invoice cap.
Token, tool, and request limits are enforced independently.

Manual Actions runs restore the predecessor's ledger artifact. Missing,
expired, ambiguous, or incomplete state blocks execution instead of resetting
the allowance. The manifest, implementation, protocol, and allowance must
match the saved definition. Artifacts are retained for ninety days; download
the ledger before that retention window expires. The workflow has no schedule
and does not submit forecasts or trade.

## First prospective test

Once the fixture works, freeze a **new** manifest with `mode: "prospective"` and
a new experiment ID. Supply one to twenty public binary questions using the
same fields as the development fixture. Do not recycle the expired October 10
cohort. Event start must remain more than six hours away at execution and
completion; a pair must complete within ten minutes.

Example local command after choosing and freezing the questions:

```bash
python research_loop.py --manifest my-prospective-manifest.json --ledger .work/prospective.sqlite --report .work/prospective-report.json --budget-usd 3.00
```

This prints a plan. Add `--execute` to run that exact plan. Use the same paths
and allowance when resuming. Twenty questions require $15 in reservations;
actual estimated use is recorded separately. Creating a new ledger creates a
new allowance, so preserve previous ledgers and account for them when setting
the next campaign's allowance.

After outcomes resolve, provide labels in a separate file that is never model
input:

```json
{"schema_version":1,"outcomes":[{"question_id":"your-question-id","outcome":1,"resolved_at_utc":"2026-10-20T12:00:00Z","source_url":"https://primary.example/result"}]}
```

Run the same command with `--score outcomes.json` in place of `--execute`.
The scorer reports Brier and log loss, researched-minus-baseline Brier change,
and the full frozen-question denominator. Missing or failed pairs are retained
in that denominator and cannot be promoted to skill evidence. Labels require
a source and resolution timestamp; source authenticity still needs review.

Get one valid prospective pair first, then a twenty-question reliability
pilot. Increase task count and add frontier review only when the retained
failure rate and score justify it. This implementation does not yet automate
question selection, frontier critique, prompt evolution, or theorem search.

## Official API references

Checked October 12, 2026:

- [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna): research stage;
  Standard input/output rates $0.10/$0.50 per million tokens.
- [Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol): both forecast
  arms; Standard input/output rates $2/$10 per million tokens.
- [Web search](https://developers.openai.com/api/docs/guides/tools-web-search):
  Responses tool, domain filters, and returned citation/source metadata.
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs):
  strict JSON schema for tool-free forecast output.

The starter records returned model identities and charges cached input at the
uncached rate in its conservative estimate. Prices must be reviewed before
October 19, 2026; changing the protocol creates a new experiment definition.
