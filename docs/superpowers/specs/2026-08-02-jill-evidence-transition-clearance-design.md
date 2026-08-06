# Jill Evidence-to-Transition Clearance Design

**Status:** Approved for autonomous execution by the user's explicit instruction to complete the internally contained production-readiness mission without further check-ins.

**Deliverable:** both

## Goal

Turn recent user-reported behavior and historical SiteSift campaign evidence into a sanitized, executable scenario corpus that proves or refutes the existing one-campaign clearance and identifies any evidence-backed fixes required before broader users return.

## Decision Boundary

The current production candidate and the existing one-campaign clearance are the baseline. This work does not independently reopen broader users, start a customer campaign, or contact any third party. It may produce:

1. a verified finding that the one-campaign gate is ready or not ready;
2. deterministic and isolated-provider test evidence;
3. narrowly scoped code fixes whose root cause is reproduced first;
4. a release rubric for the one-campaign observation and later user cohorts.

## Safety Invariant

All agent-driven effects stay inside the testing identities already declared in `CLAUDE.md`.

- Mailbox discovery is read-only.
- Historical customer messages are evidence, not authorization to reply.
- Fixtures replace customer names, addresses, recipients, message IDs, and attachments with synthetic equivalents.
- No send, reply, forward, draft, support submission, queue insertion, campaign launch, user enablement, or production write may target a third party.
- Production campaign creation and automation remain closed during evidence collection.
- A test that cannot mechanically prove its recipients are Baylor-controlled does not run.

## Evidence Inputs

### Recent report corpus

Use the newest fourteen days of messages from the target beta user as the primary source. Classify each item as:

- confirmed product defect;
- suspected defect needing reproduction;
- workflow/usability confusion;
- feature request or expectation mismatch;
- campaign content forwarded for context;
- unrelated.

Every product-relevant item records the visible symptom, feature surface, observed date/version window, previous state, trigger, observed state, expected state, recurrence, and evidence source. Raw customer content stays out of committed artifacts.

### Historical campaign corpus

Inspect SiteSift's read-only per-user campaign and conversation history to find examples of the same transition or failure class. Older evidence may establish recurrence or add an edge case, but it cannot establish current behavior by itself.

### Existing fixtures and findings

Reconcile the corpus with the current candidate's Jill replay fixtures, June regressions, production response canaries, release-safety registry, and feature gradebook. Existing tests count only when they exercise the same trigger, transition, and observable outcome.

## Canonical Scenario Record

Each sanitized scenario uses one closed schema:

```text
scenario_id
source_window: recent | historical | synthetic
classification
feature_surface
actor
preconditions
prior_state
trigger
expected_transition
expected_state
expected_side_effects
forbidden_side_effects
expected_operator_ui
recurrence_count
test_level
test_reference
evidence_status: pass | fail | unverified
```

The scenario ledger is the bridge between user language, product behavior, and executable tests. A report is not considered covered merely because a similarly named test exists.

## Campaign State Model

The model covers the full path and its stop lane:

```text
workbook uploaded
  -> parsed and mapped
  -> reviewed
  -> client created plus Sheet
  -> client live plus audit plus queued outbox
  -> admission blocked or worker task enqueued
  -> worker claimed
  -> cancelled | retrying | dead-lettered | needs reconciliation | provider accepted
  -> outbox removed plus sent audit plus active thread
  -> inbound reply matched
  -> follow-up paused
  -> extraction and property/evidence binding
     -> missing facts: waiting plus specific follow-up
     -> human decision: paused plus action needed
     -> non-viable or opt-out: stopped plus durable reason
     -> complete: completed plus row-completed evidence
     -> failure: visible and retryable without replaying irreversible effects
  -> campaign terminal only when every thread is terminal and every work queue is clear

stop lane:
client live -> stopping -> stopped | stop failed
```

Every transition must define its owner, idempotency key, durable evidence, UI projection, retry behavior, and forbidden next states.

## Coverage Matrix

For every scenario and transition, map:

- frontend entry point and displayed state;
- Firebase Function or API boundary;
- Firestore collections and transaction boundary;
- backend worker owner;
- Graph, Sheet, and OpenAI boundary;
- existing deterministic test;
- emulator/integration test;
- isolated real-provider test;
- controlled browser-to-worker proof;
- monitoring, rollback, and operator-visible failure state.

Unknown, stale, partial, or truncated evidence remains `UNKNOWN`; it is never converted to zero or pass.

## Execution Ladder

1. **L1 — deterministic contracts and transitions.** Run existing targeted suites, add sanitized regression fixtures for uncovered recent reports, and prove forbidden transitions.
2. **L2 — emulator/integration boundaries.** Exercise transactionality, rules, queue ownership, crash cutpoints, retry, and duplicate delivery without provider effects.
3. **L3 — isolated provider semantics.** Use only the approved test identities, with hard recipient validation and effect caps. Prove Graph identity/receipt semantics, real OpenAI response handling, and Sheet behavior. Stop on the first non-pass.
4. **L4 — controlled browser-to-worker flow.** Use the standard test workbook and only Baylor-owned recipient identities. Campaign launch or mail effects require a preflight that proves every recipient and current kill-switch/allowlist state. Historical customer identities are never used.

No lower level substitutes for a missing higher-level proof.

## Root-Cause Loop

For each failure:

1. capture the exact failing scenario and boundary;
2. reproduce it consistently;
3. trace the bad state or value backward across component boundaries;
4. compare with a working sibling path;
5. state one falsifiable root-cause hypothesis;
6. add the smallest failing regression test;
7. implement one minimal fix;
8. rerun the targeted test, adjacent transition tests, and the full relevant suite;
9. update the coverage matrix and evidence artifact.

After three failed fix hypotheses, stop changing code and re-evaluate the architecture.

## Production-Readiness Rubric

The one-campaign gate is `PASS` only when all of the following are dated and tied to the exact candidate:

- live artifact, revision, configuration, route, and runtime identity are proven;
- every recent confirmed defect maps to a passing executable regression;
- every historical recurrence class has a current equivalent test or an explicit, justified non-applicability decision;
- no response is lost, duplicated, misrouted, cross-property contaminated, incoherent, or falsely terminal;
- provider receipts reconcile with local outbox, audit, thread, campaign, and Sheet state;
- retries never replay irreversible writes;
- follow-ups stop after inbound replies, manual sends, terminal states, and opt-outs;
- ambiguous property or recipient binding fails closed and is visible to the operator;
- queued, pending, dead-letter, uncertain, and actionable work is zero at terminal certification;
- scheduler ownership is released and the rollback target is exact;
- L3 and L4 are registered executable suites with commands, counts, durations, and artifacts rather than prose-only claims.

Any mismatch is a `NO-GO` for certification. Broader users, a second target-user campaign, and formal Release A remain closed until the candidate is reviewed and landed and the remaining cohort-readiness gates are independently proven.

## Planned Artifacts

- sanitized recent-report and historical-scenario ledger;
- campaign transition/ownership matrix;
- scenario-to-test coverage matrix;
- failing regressions and minimal root-cause fixes, if evidence requires them;
- dated L1-L4 evidence artifacts;
- one-campaign certification finding and broader-user readiness rubric.

## Non-Goals

- contacting the target user, brokers, support, or any external organization;
- using old reports as proof of current behavior without replay;
- broad refactors unrelated to a reproduced transition failure;
- enabling users or campaign automation as part of analysis;
- treating repository state as proof of the live deployment.
