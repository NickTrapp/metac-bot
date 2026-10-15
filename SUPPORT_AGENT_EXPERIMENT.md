# Support experiment preparation

This branch adds a public-safe readiness scaffold. It contains no support
conversations, private evaluation data, or paid API implementation.

The initial branch push runs offline tests and reports only the Boolean
`openai_api_key_present`. This checks whether GitHub supplies the existing
repository secret; it does not contact OpenAI or establish that the key is valid.
The value is never printed, copied to a file, or transferred to another machine.
The secret is scoped to the final presence-check step. No packages are installed.
Later workflow runs and reruns are blocked.

`python3 support_agent_readiness.py` prints an offline plan. `--execute` always
stops with `paid_execution_not_enabled`. The scaffold creates no reservation.

## Before a separate paid workflow can be added

- Freeze and review the exact public-safe experiment, model, prices, code,
  input hashes, output limits, request count, and expected cost bound.
- Allocate no more than $5 from the existing shared spending-window allowance.
  Preserve all prior unknown liabilities and concurrent allocations. Local
  reservation amounts are not provider invoice caps, and missing usage is
  never zero. Do not reset accounting by creating a new experiment or ledger.
- Add focused offline tests for the final transport, accounting, and redaction.
- Use a new exact-path, trigger-only commit with a pinned prepared parent commit,
  manifest hash, experiment ID, first-run/first-attempt checks, and complete
  Actions history verification. Keep the shared concurrency group.
- Default to no tools, no automatic retries, no secret-bearing logs, and no
  raw API errors. Retain only reviewed public-safe outputs and usage fields.

There is currently no paid trigger or paid workflow. Existing forecasting
workflows and the separate research-loop draft are unchanged.
