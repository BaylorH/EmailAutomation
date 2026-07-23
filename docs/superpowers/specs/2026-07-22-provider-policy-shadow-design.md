# Bounded Provider-to-Policy Shadow Design

## Goal

Prove that fresh, pinned-provider interpretations of sanitized broker evidence
can pass through claim validation and the deterministic policy evaluator without
changing any campaign, sheet, mailbox, follow-up, recipient, or production
state.

This gate tests the intended evidence -> claims -> policy architecture. It does
not claim to replay the deployed legacy `updates` / `events` /
`response_email` prompt.

## Decision

Use the existing pinned claim-proposal prompt and provider transport, then feed
the accepted claims into the deterministic policy evaluator.

Rejected alternatives:

- Refactoring and importing the live legacy prompt now would widen the trusted
  surface before the disabled effect adapter exists.
- A new legacy-shaped test prompt would prove an invented prompt, not deployed
  behavior or the approved replacement architecture.

## Scope

Included:

- Eight existing sanitized provider-quality cases with honest evidence-to-policy
  meaning.
- Existing evidence normalization, entity resolution, pinned proposal adapter,
  claim validation, and deterministic policy evaluation.
- Exact policy decision and action-signature oracles.
- Provider-quality validation before policy grading.
- One smoke call followed by three full repeats only after the smoke passes.
- Privacy-safe identity, telemetry, repeatability, and gap reporting.

Excluded:

- Jill, customer, mailbox, sheet, production, or live-campaign data.
- Service imports, persistence, sends, scheduling, follow-up mutation, or other
  effects.
- Browser automation and deployment.
- Claims that the pinned claim prompt is the deployed legacy proposal prompt.
- Weakening an oracle when a fresh output exposes a policy or extraction gap.

## Corpus

The strict shadow catalog references existing provider-quality cases rather
than duplicating broker evidence. The initial bounded set covers:

- fresh evidence over quoted history;
- split-suite isolation;
- wrong-property attachment isolation;
- complete property facts and terminal closeout;
- referral, opt-out, call, tour, brochure, remediation, and return-date intents;
- a broker correction that supersedes a prior rent;
- repeated information requests;
- explicit target unavailability plus stopped follow-ups.

Each shadow case supplies only a policy contract, selected entity relationships,
current state, exact expected decisions, required/forbidden action signatures,
and any expected policy-gap codes. The provider-quality catalog remains the
claim oracle.

## Composition Boundary

For each case and repeat:

1. Normalize the saved sanitized message and resolve entities.
2. Build the existing strict claim-extraction request.
3. Invoke either the recorded adapter or the pinned provider adapter.
4. Reconcile transport telemetry independently of adapter declarations.
5. Validate the complete provider claim/review result against the existing
   provider-quality oracle.
6. Combine accepted prior and fresh claims for the selected policy entities.
7. Evaluate the immutable campaign contract and current-state snapshot.
8. Compare exact decisions and action signatures with the shadow oracle.

Provider-quality failure prevents a policy pass. Review-only or malformed
outputs never become policy facts.

## Gap Contract

The shadow distinguishes three outcomes:

- `pass`: provider quality and exact policy behavior both match.
- `expected_gap`: extraction is correct, but the current policy planner omits a
  named behavior that the fixture intentionally exposes.
- `blocker`: extraction mismatch, unexpected policy behavior, unsafe approval
  boundary, entity collapse, telemetry failure, unclassified difference, or
  semantic variance.

Tour and information-request claims are currently extracted but not action
planned. Their absence must appear as named gaps; it cannot be hidden by an
oracle that simply ignores those predicates.

## Call, Token, And Spend Bounds

- Smoke: one call for `target-unavailable-stop-followups`.
- Final gate: eight cases x three repeats = 24 calls.
- Total authorized maximum: 25 calls.
- Retry count: zero at both SDK and harness layers.
- Model, SDK, prompt, schema, timeout, and maximum output tokens remain pinned.
- Before each invocation, reserve a conservative upper bound using UTF-8 input
  bytes as the maximum text-token count, a fixed 4,096-token allowance for
  request framing and tokenizer overhead, the transport's pinned maximum output
  tokens, and pinned list prices.
- Refuse the call if the next reservation would exceed 1,500,000 tokens or
  5,000,000 micro-USD. Report both conservative reservations and observed
  provider usage.
- Stop immediately after any failed smoke, incomplete usage, unexpected call
  count, malformed output, provider-quality mismatch, blocker, or semantic
  policy variance.

The generous hard ceilings bound worst-case exposure; actual usage is expected
to be much lower and is reported exactly.

## Report Contract

Reports may contain only:

- safe case IDs and gap/error codes;
- code, source, fixture, prompt, dependency, and result hashes;
- model/provider/runtime identities;
- decision-state and action-signature enums;
- counts, digests, timing, token, call, and micro-USD totals.

Reports must not contain evidence text, message bodies, addresses, email
addresses, recipient values, claim values, or raw model output.

## Proof Gates

1. Strict fixtures reject unknown keys, duplicate cases, unsupported selectors,
   raw evidence, addresses, emails, values, and references outside the existing
   provider-quality catalog.
2. Recorded mode proves exact composition, prior-claim handling, entity
   isolation, gap classification, order independence, and zero provider usage.
3. The runner rejects provider mode without explicit opt-in or with any call,
   token, or spend plan above the hard bounds.
4. The one-call smoke passes before the 24-call final gate can start.
5. Three provider repeats have stable semantic policy outcomes; wording-only
   proposal variance is nonblocking.
6. Focused tests, isolation checks, compilation, and the full backend suite pass.

## Stop Conditions

Stop without further provider calls if evidence mapping is dishonest, the prompt
boundary is mislabeled, source state is dirty at the final evidence run, the
transport is unpinned, telemetry is incomplete, a cap cannot be proved before a
call, provider quality fails, entities collapse, unsafe behavior varies, an
oracle is weakened, or any effect/service import enters the shadow surface.

## Later Gates

After this proof, design the disabled effect adapter against the stable policy
contract. Then test dry effect planning, staging persistence, browser-visible
admin evidence, and only finally a separately approved production canary.
