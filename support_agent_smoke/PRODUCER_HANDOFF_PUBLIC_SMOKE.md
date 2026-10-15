# Public-smoke subset of the producer handoff

Origin: frozen Support Agent Lab v0.2, run ID sal-20261015-v02,
source commit 672fc801df52d85fc6e681601b737ceae5ac9fd5.
This is an execution summary restricted to the public smoke. The original
private handoff, private test fixtures, and grader are intentionally omitted.

Execute the first two public golden cases, G01 and G02, paired across direct
and procedure modes. They have one turn each, so the selected smoke has four
maximum generation calls. Pair order is direct/procedure for G01, then
procedure/direct for G02. The only candidate-mode difference is the procedure
instruction. Keep fake commerce actions and public synthetic observations.

Pinned model: gpt-5.4-mini-2026-03-17 via Responses, reasoning xhigh, 8,192 maximum output tokens,
16,000 maximum input tokens, at most four model calls per episode, and a
120-second per-request timeout.
The smoke allocation is $0.25 inside the same $5 experiment reservation, not
extra spending. The full $4.75 private stage is not implemented in this package.
The current shared authorization is $100 over the approved new 30-day window;
older calendar-month wording in the producer handoff is superseded.

Official standard rates verified during preparation on 2026-10-15:
$0.75 input, $0.075 cached input, $4.50 output per million tokens.
https://developers.openai.com/api/docs/models/gpt-5.4-mini
Recheck official rates immediately before the trigger is admitted.

The original candidate source is explicitly adapted with reviewed admission
safety fixes and a no-install stdlib transport. Source hashes and the patch are
included. Do not describe this adapted smoke as an untouched frozen full test.
Actual live responses, model accessibility, key validity, and billed usage
have not been observed. No live test has been executed.
