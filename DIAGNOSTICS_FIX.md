# Offline-only request diagnostic fix, 2026-10-10

## Scope

This copy adds public-safe diagnostics to the prospective request handler. It
does not establish why the original five research requests failed. No new pilot
was run, and the original source and failed-run artifacts were not edited.

Only `prospective_forecast.py`, `tests/test_prospective_diagnostics.py`, and this
document differ from the source copy. Requests, prompts, model, frozen questions,
protocol, deadline and one-shot gates, reservation, search/request allowances,
zero retries, and failed-arm handling are unchanged. This patch does not grant
permission to rerun or publish anything.

## Retained fields

Every attempt starts with null `failure_diagnostic`, null `response_id`, and
`response_usage_metadata` whose input/output counts start as null.

On failure, `failure_diagnostic` contains exactly:

- `phase`: `sdk_request`, `response_metadata`, `response_validate`, or
  `source_validate`
- `response_received`: whether the SDK create call returned, before local
  metadata/response/source validation; an HTTP-error exception can have a known
  status with this flag false
- `exception_class`: a fixed allowlist of local classes or qualified OpenAI SDK
  API error names; unknown classes are null
- `http_status`: only an exact integer from 100 through 599, otherwise null
- `provider_code`: only an exact string in the small fixed allowlist in the
  source, otherwise null

Exception strings, representations, bodies, messages, headers, requests, and
tracebacks are never read or included by the new diagnostic code. No attempt is
made to infer a provider code from free text or a response body. Attribute
getter failures do not replace the original failure. The existing fixed public
failure message and no-retry behavior remain unchanged.

Immediately after the SDK returns, before `_read_response`, a response ID is
retained only if it matches `resp_` plus 1 to 192 ASCII letters, digits,
underscores, or hyphens. Input/output counts are retained independently only if
they are exact nonnegative integers. Booleans, numeric strings, floats, and
negative values remain unknown/null. A malformed output can therefore retain
safe metadata without becoming an accepted response.

These metadata counts never populate `usage_observation`, calculate cost,
count searches, or make usage complete. Existing measured-usage validation and
cost accounting are unchanged. A returned response does not establish a charge,
and missing information is not a zero.

## Verification

The local stdlib suite passed 166 tests, including ten new tests. A second full
run also passed with socket connect, connect_ex, and create_connection denied
in the test process. All provider clients in this suite are synthetic mocks.

The new tests distinguish:

1. Local NameError during the SDK request
2. A mocked HTTP 400 and allowlisted SDK error code
3. Malformed response output after the SDK returned, retaining safe ID/counts
   while leaving accepted usage unknown
4. A source-domain validation failure, retaining already measured usage
5. Malformed response metadata and unknown exception classes/codes

They also check malicious exception-message suppression in final results and
durable checkpoint snapshots; forbidden exception str/repr/body/message/request/
response access; exact-type/range allowlists; hostile attribute getters; no
retry; retained denominator; and original reservations/limits.

Run the portable stdlib tests from this directory:

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python prospective_pilot.py --validate-manifest
```

Syntax checks and the five-question frozen-manifest check also passed. The
protocol hash remains
`74bb67cde46bbb05a4770a11f0ef7f3edb3221863b9937ef1af4caff103a4400`.

## Limits

- This only improves future observability; it cannot recover discarded original
  exception details, response IDs, usage, or billing information
- Mock HTTP 400 is a negative-path test, not evidence that the original API
  returned HTTP 400
- No paid calls, installs, credential access, remote changes, pilot trigger, or
  retry were performed
- OpenAI is not installed in this local interpreter. The separate locked
  real-SDK MockTransport check was not run, and no live provider compatibility
  claim is made
- Logging is intentionally lossy: unrecognized exception classes/codes and IDs
  outside the conservative shape remain null. SDK request phase alone cannot
  distinguish local pre-send failure from provider/network failure
- The phase covers request handling, not later forecast JSON parsing or durable
  checkpoint failures. Those behaviors are unchanged
