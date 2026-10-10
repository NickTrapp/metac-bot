# One approved test-area forecast

The first synthetic OpenAI test passed in
[run 38017773835](https://github.com/NickTrapp/metac-bot/actions/runs/38017773835).
The next, separately approved step is exactly one forecast with its reasoning
in Metaculus's designated bot testing area. No recurring runs or scored
competition entry are enabled.

## Boundaries

- Manual workflow only: `Metaculus test-area end-to-end check (one shot)`
- Only recovery run number 4, attempt 1, on the reviewed main branch is allowed.
  The first two dispatches stopped at preflight: run [38018946468](https://github.com/NickTrapp/metac-bot/actions/runs/38018946468)
  omitted the checkbox; run [38019014010](https://github.com/NickTrapp/metac-bot/actions/runs/38019014010)
  hit the original one-shot lock. Neither reached any OpenAI or Metaculus call
- Run [38019568451](https://github.com/NickTrapp/metac-bot/actions/runs/38019568451/job/114117334084)
  passed authentication and dependency checks but stopped in question selection.
  Its source commit is `31f3f2b5ab2c50f4d1ee8355c85faaf47fe6bdf5` and its exact
  reviewed terminal diagnostic at 2026-10-10 03:10:25 UTC was:
  `No eligible, previously unforecasted binary test-area question was found.`
  In that immutable source, `select_target` must return before `RequestBudget`
  construction, `make_forecast`, or `post_once`. Thus this specific reviewed run
  made read-only Metaculus requests but no OpenAI call or forecast/comment write
- Before execution, the gate requires complete history containing exactly those
  three pinned failures plus the current run. It rechecks each exact job ID,
  source commit, attempt and all step outcomes. The run-3 exception relies on the
  explicit log/source reconciliation above; a failed execution step alone would
  never prove no spend or submission. Missing evidence, another attempt, extra
  run, changed metadata, or a different failed run blocks
- The manual workflow no longer asks for the redundant checkbox: the owner has
  already approved exactly one test-area forecast and private explanation.
  Select this workflow on main and choose Run workflow once. Do not use Re-run jobs.
  No later run, rerun, or generic failed-run exemption is allowed
- The successful first run is pinned by run ID and source commit. Its full USD 1
  reservation remains counted. The new workflow reserves at most USD 1 for two
  OpenAI request attempts, so cumulative reservations are at most USD 2 against
  the original USD 10 authorization. Neither token limits nor a timeout are
  presented as a provider/account-wide financial cap
- Same direct OpenAI model, price check, two-attempt limiter, and no-retry parser
  as the original test. One prediction, no external research or provider fallback
- Existing repository secrets only. No credentials, private profile, generated
  forecast, or reasoning are printed in public Actions logs

## Sequence

1. GET the bot's own identity. Require a positive ID internally, a bot account,
   active/non-spam status, and account-level API forecasting access enabled
2. GET the project by exact slug `bot-testing-area`; pin its returned numeric ID
3. Discover open binary questions, then validate each full detail response.
   Require this test project as default and reject visible outside scoring or
   unknown project memberships. Well-formed category/topic/news-category labels
   are allowed only under their matching type keys, without scoring metadata. Require a standalone approved, open, unresolved binary question,
   valid current timing, and explicit forecasting permission
4. Select deterministically by ascending post ID, requiring empty own forecast
   history. A missing/inaccessible history response does not count as empty.
   Emit only candidate counts and fixed rejection codes before any paid request;
   no raw question text, account profile, private forecasts or API error bodies
5. Generate one forecast and parse it with the existing guarded OpenAI wrapper
   using the question's actual criteria when present. Empty string criteria are
   valid in Metaculus's schema and remain empty; missing rules are never invented
6. Re-fetch the project, question, permissions and own history before writing.
   Abort if anything material changed; do not switch questions after paid work
7. Send exactly one request to the official atomic forecast+comment endpoint.
   Its one forecast and one private comment are saved in one server transaction
   The comment explicitly labels the result as an integration test, not calibrated research
8. Read back both the bot's forecast and its matching private explanation. Check
   author, destination, probability, timestamps, unique run marker, privacy and
   included forecast. Print separate verification booleans and usage totals only

No HTTP redirect or request retry is used for Metaculus. The write-attempt guard
is consumed before sending. A timeout, lost response, 5xx, or malformed response
can mean the write happened. GET-only reconciliation follows; any unresolved or
partially verified outcome must never cause automatic resubmission. Even a 201
response is followed by both readback checks. No fallback to separate writes.

Metaculus's official resources state that the designated testing-area questions
are not scored and do not affect bot ranking. The API exposes only memberships
visible to the bot; the checks above do not claim to discover hidden memberships.
Authentication normally updates the token's last-used timestamp. No account
settings, credentials or permissions are changed.

## Selector correction and diagnostic limits

The original guard confused taxonomy with competition membership: Metaculus puts
category, topic and news-category labels in the same `projects` object as actual
competitions. Source-derived regression fixtures reproduce that defect; they are
not claimed to be captures of the live testing-area JSON. The exact cause of the
run-3 failure remains unknown because its selector suppressed per-candidate
reasons. The next run will report bounded, fixed-code counts before paid work.
No guard is relaxed for outside tournaments, other question series, indexes,
site-main, communities, leaderboard tags, unknown types or malformed metadata.
The official Question model allows blank resolution criteria. This test accepts
that schema-valid empty string while retaining a nonblank title, correct field
types, open dates/status, permissions and exact testing-area ownership.

## Offline checks

`python -m unittest discover -s tests -v`

The identity and end-to-end tests use fake connections, fake SDK responses, and
synthetic fixtures. They do not read local secrets or call any API. The workflow
also runs the existing dependency-backed offline format checks before execution.

## Official sources

- [Testing-area guidance](https://www.metaculus.com/notebooks/38928/bot-tournament-resources-page/)
- [Project type definitions](https://github.com/Metaculus/metaculus/blob/6b3f156486108b05ecff1df46cdb7379d519a91b/projects/models.py#L24-L44)
- [Taxonomy and project serializers](https://github.com/Metaculus/metaculus/blob/6b3f156486108b05ecff1df46cdb7379d519a91b/projects/serializers/common.py#L60-L181)
- [Project endpoints](https://github.com/Metaculus/metaculus/blob/main/projects/urls.py)
- [Identity GET](https://github.com/Metaculus/metaculus/blob/main/users/views.py)
- [Identity fields](https://github.com/Metaculus/metaculus/blob/main/users/serializers.py)
- [Question model with optional blank criteria](https://github.com/Metaculus/metaculus/blob/main/questions/models.py)
- [Atomic forecast and comment handler](https://github.com/Metaculus/metaculus/blob/main/questions/views.py)
- [Forecast readback format](https://github.com/Metaculus/metaculus/blob/main/questions/serializers/common.py)
- [Comment and included-forecast format](https://github.com/Metaculus/metaculus/blob/main/comments/serializers/common.py)

Successful testing does not enable the disabled legacy runners or schedules.
Do not copy/rename workflows or override their guards to obtain extra runs.
