# Workflow Provider Reliability Gate Design

## Goal

Determine whether the earlier intermittent workflow-intent extraction failure
is reproducible provider variance or a stable defect, using a small fixed
sanitized corpus and no workflow effects.

## Decision

Add one fixed `workflow-reliability` mode to the existing provider-policy
runner. It selects three existing cases and runs each four times:

- `workflow-intents-visible`: compound referral, call, tour, information,
  remediation, return-date, and opt-out semantics;
- `repeated-information-request`: repeated brochure-request semantics and
  action deduplication;
- `unavailable-optout-suppression`: terminal control case.

This produces exactly 12 planned calls. The runner remains fail-fast, so a
mismatch can stop the run before all 12 calls are spent.

Rejected alternatives:

- Rerunning the full 8 x 3 gate spends half its calls on cases that were already
  stable and does not focus evidence on the observed failure.
- Repeating only the compound workflow case provides no control for provider,
  transport, or policy-wide drift.
- A free-form case/repeat CLI makes the evidence hard to compare and allows an
  operator to accidentally exceed the intended experiment.

## Budget

The new mode has mechanically distinct ceilings:

- calls: 12;
- conservatively reserved tokens: 400,000;
- conservatively reserved cost: 2,500,000 micro-USD ($2.50);
- retries: zero.

Each call reserves UTF-8 input bytes, 4,096 framing/tokenizer overhead tokens,
and the pinned maximum output before invocation. The hard ceilings describe
worst-case authorization, not expected usage. Actual usage and spend remain
independently observed and reported.

The existing `smoke` and `final` schedules and ceilings do not change. The
previous 25-call authorization is exhausted; implementing or recorded-testing
this mode does not authorize an OpenAI call.

## Gate Semantics

Every attempted sample must pass, in order:

1. pinned provider/model/prompt/runtime/source identity;
2. complete independent transport usage telemetry;
3. exact provider-quality claim/review oracle;
4. exact deterministic policy decision and action oracle;
5. zero workflow gap codes;
6. stable semantic policy digest across repeats.

The runner stops after the first provider-quality mismatch, policy blocker,
semantic variance, incomplete usage, or budget refusal. A partial run fails;
later clean diagnostic samples cannot average away the failure.

## Report And Privacy

The existing value-free report contract is unchanged. Reports contain safe
case IDs, mismatch/gap codes, hashes, enum projections, call/token/cost counts,
and timings. They contain no evidence text, message body, address, email,
recipient, claim value, or raw model output.

## Scope Boundary

This gate imports no Graph, Firebase, Sheets, mailbox, queue, drafting, send,
browser, deployment, or production surfaces. It uses only existing sanitized
fixtures. Provider execution remains blocked until Baylor approves the exact
12-call / 400,000-token / $2.50 ceilings.

## Proof

- Parser accepts only `smoke`, `final`, and `workflow-reliability` modes.
- Recorded reliability mode produces exactly 12 passing results over exactly
  the three fixed case IDs with zero usage and zero gaps.
- Identity carries the mode-specific call/token/spend caps and selected-catalog
  hash.
- OpenAI mode still requires explicit call opt-in, clean committed source,
  API key, and a matching budget transport.
- Unexpected schedules fail before transport construction.
- Existing smoke/final behavior, privacy tests, isolation, compilation,
  focused tests, and the full backend suite remain green.

## Stop Condition

Stop before any provider call after the recorded build proof. The next step is
an explicit budget decision, not automatic execution.
