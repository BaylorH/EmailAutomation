# No-Effect Legacy Shadow Comparison Design

## Goal

Compare the deterministic claim-policy action plan with what the legacy
`updates` / `events` / `response_email` proposal path would attempt, without
calling providers, importing worker services, touching customer data, or
executing effects.

The output is a release-grade discrepancy report. It is not an alternate
production executor and does not claim that synthetic cases are historical
observations.

## Why This Gate Exists

The legacy path has two independently evolving decision surfaces:

1. Proposal updates are applied to the current sheet row before events run.
2. Events mutate lifecycle state and may suppress a reply.
3. A later response selector can still draft or send based on row viability and
   missing fields.

That shape can hide compound failures. A correct reply draft does not prove the
right row was updated, and a correct event does not prove follow-ups were
frozen or a recipient change was held for approval. The shadow compares these
attempts separately.

## Scope

Included:

- Strict, sanitized legacy proposal projections with explicit provenance.
- The existing 22-case deterministic policy lattice as the policy oracle.
- Normalized attempted actions for row facts, terminalization, review,
  recipient changes, alternate properties, call/tour requests, and outbound
  drafts.
- Explicit entity binding for every legacy update and event.
- Deterministic discrepancy grading and a privacy-safe JSON report.
- Repeatability, fixture-order independence, isolation, and fail-closed schema
  tests.

Excluded:

- Provider calls or fresh LLM inference.
- Imports from `processing.py`, `ai_processing.py`, Firestore, Sheets, Graph, or
  any send/scheduler module.
- Production reads, writes, sends, deploys, or Jill campaign interaction.
- Claiming that a synthetic boundary fixture is a recorded production result.
- Executing either the legacy or new action plan.

## Provenance Contract

Every shadow case declares one source kind:

- `historical_probe`: a sanitized projection of a recorded live or replay
  result documented in the repository.
- `legacy_test_contract`: behavior already asserted by a named legacy test.
- `synthetic_boundary`: an intentionally constructed proposal used to prove a
  comparison invariant.

Each case names its source reference and the policy case it compares against.
Reports contain only safe case IDs, source references, hashes, counts, and
discrepancy codes. Proposal values, email bodies, addresses, and recipients are
never emitted.

## Fixture Shape

The strict fixture catalog stores:

- `policyCaseId`: an existing policy-lattice case.
- `provenance`: source kind plus a report-safe repository reference.
- `bindings`: the current-row entity, one entity per event, and whether any
  suggested recipient is the same, different, or absent.
- `legacyProposal`: sanitized updates, events, presence/absence of a response
  draft, and the recorded `skip_response` state.
- `expected`: exact overall disposition, severity, and discrepancy codes.

Updates carry only a canonicalizable column name and a redacted scalar type.
Events carry only type and a controlled reason token. This is enough to model
attempted behavior while preventing fixture data from becoming a second copy
of customer content.

## Normalized Legacy Attempts

The adapter projects proposals into stable action attempts:

- each update -> `fact_update:automatic` on the bound current entity;
- `property_unavailable` -> terminal status, follow-up freeze, and row move;
- `close_conversation` -> terminal status and follow-up freeze;
- `contact_optout` -> terminal status, follow-up freeze, row move, and review;
- `new_property` -> `alternate_property_proposal:human_required`, plus a human
  recipient change when its contact differs;
- `wrong_contact` -> human recipient change and review;
- `needs_user_input` -> review;
- `call_requested` -> human call request and review;
- `tour_requested` -> human tour request and review;
- present, unsuppressed `response_email` -> `outbound_draft:automatic`.

Attempts include a normalized entity key, action type, approval class, and a
small semantic qualifier. They never include proposal values.

## Comparison Rules

Every difference is classified; no ungraded differences are permitted.

### Equivalent

The legacy attempt and policy obligation match at the safety-relevant level,
including entity, action family, approval boundary, and fact field.

### Expected Improvement

The new policy adds a safety-preserving state or review obligation that the
legacy proposal surface cannot represent, such as waiting through an
out-of-office return date. These are visible but do not block this gate.

### Legacy Safety Risk

Release blocker. Examples:

- a legacy fact mutation is absent from the policy plan;
- a legacy terminal event conflicts with a nonterminal policy decision;
- market availability is collapsed into fit failure;
- a target row is mutated for an alternate-property claim;
- a required review or recipient approval is bypassed;
- an outbound draft remains automatic during opt-out or mandatory review;
- terminal policy intent lacks a legacy follow-up freeze.

### New Policy Gap

Release blocker. The policy plan omits a safety obligation established by the
legacy contract or cannot distinguish a required action. The shadow never
weakens the policy oracle to make this category disappear.

### Deferred Surface

Nonblocking but explicit. Outbound wording and sheet row placement are not yet
fully modeled by the policy planner. A legacy closing draft or row move is
reported here when it does not violate a safety boundary. Deferred items remain
visible for the later draft/effect-adapter gates.

## Severity And Gate

- `none`: no discrepancies.
- `info`: only expected improvements.
- `warning`: one or more deferred surfaces, no safety issue.
- `release_blocker`: any legacy safety risk, new policy gap, hidden difference,
  invalid fixture, entity-binding ambiguity, or expected-result mismatch.

The report passes when every case matches its exact expected classification,
all differences are graded, repeat runs are byte-stable, and no report payload
contains proposal values.

## Architecture

New pure modules under `email_automation/claim_pipeline/`:

- `legacy_shadow_fixtures.py`: strict loader, provenance validation, manifest
  hashing, and cross-checks against the policy catalog.
- `legacy_shadow.py`: policy-case materialization, legacy attempt projection,
  discrepancy grading, report contracts, and deterministic execution.

New script:

- `scripts/run_claim_pipeline_legacy_shadow.py`: records code revision, source
  hash, dirty-tree state, fixture hashes, repeat count, and emits a compact JSON
  report. It has no provider or effect option.

The package isolation test continues to reject service imports. The public
package boundary exposes only the pure fixture and shadow APIs.

## Proof Gates

1. Strict schema rejects unknown keys, unsafe provenance, raw response bodies,
   recipient values, missing entity bindings, and unknown policy cases.
2. Opposed cases prove wrong-row, tour-only, fit-vs-market, split-suite,
   alternate-property, redirect, review, opt-out, terminal-freeze, and closeout
   behavior.
3. Three runs plus reversed fixture order produce one digest.
4. Source imports remain inside the isolated claim-pipeline allowlist.
5. Report scanning finds no proposal values, addresses, email addresses, or
   message bodies.
6. Focused tests and the full backend suite pass.

## Stop Conditions

Stop and fail the gate if the implementation imports a service module, executes
an effect, needs raw customer data, guesses an entity binding, hides a
difference, classifies a safety-critical discrepancy as harmless, or modifies
the deterministic policy oracle merely to match legacy behavior.

## Next Gate

After this recorded/synthetic no-effect gate passes, the next provisional step
is a bounded provider-backed shadow that produces fresh proposal projections
for the same sanitized corpus. Only after both gates agree should an effect
adapter be designed, still disabled by default and tested outside production.
