# Disabled Effect Adapter Design

**Date:** 2026-07-22
**Status:** Approved by Baylor on 2026-07-22
**Deliverable:** Finding
**Project:** SiteSiftAI / MOHR Email Automation

## Goal

Define a pure, disabled-by-default boundary that evaluates whether validated
`ActionPlan` actions would be eligible for a future effect without importing,
calling, or simulating any production service. The boundary must fail closed on
stale state, missing approval, duplicate effects, dependency failures, terminal
suppression, unsupported actions, or malformed execution context.

This design does not connect the claim pipeline to the legacy worker. It does
not write Firestore or Google Sheets, call Microsoft Graph, schedule follow-ups,
queue responses, create notifications, draft mail, send mail, or change any
production configuration.

## Current Boundary Map

The deterministic claim-policy package already provides the immutable inputs
needed at an effect boundary:

| Existing contract | Relevant guarantees |
|---|---|
| `DecisionSnapshot` | Stable decision identity, campaign and entity binding, contract version, source snapshot hash, terminal/review state |
| `ExecutionScope` | Tenant, client, campaign, thread, Sheet, and row anchor |
| `PlannedAction` | Stable action ID and effect idempotency key, source claims, expected prior state, approval class, dependencies, sequence, recipient, bounded payload |
| `ActionPlan` | Stable plan identity over the ordered action IDs and source decision |
| `validate_action_plan` | Tenant/campaign/scope bindings, exact decision/snapshot binding, unique identities and sequences, source-claim provenance, payload allowlists, recipient rules, required prior state, and dependency ordering |

The current `EffectReceipt` and `CommitReceipt` are future-facing general
contracts. `EffectReceipt.status` permits `applied`, so those types must not be
used to represent this dry-run gate. Doing so would make a no-effect report
indistinguishable from evidence of a real mutation.

### Legacy Effect Seams

The legacy `process_inbox_message` path currently performs many effects inside
one orchestration function. These seams are inventory only; the new adapter
must not import them.

| Effect family | Current legacy seams | External surface |
|---|---|---|
| Message acquisition and reply | Graph message reads in `process_inbox_message`; `send_reply_in_thread` creates, patches, sends, and indexes replies | Microsoft Graph, Firestore |
| Sheet fact updates | `apply_proposal_to_sheet`, AI metadata writes, comment appends, formula refresh, rollback | Google Sheets, Firestore audit |
| Property lifecycle | `move_row_below_divider`, replacement-row insertion, row-number synchronization, row highlighting | Google Sheets, Firestore threads |
| Attachment routing | flyer/floorplan link appenders, property-image writes, message attachment metadata | Google Sheets, Drive links, Firestore |
| Conversation state | `update_thread_status`, direct thread patches, `complete_threads_for_row`, `stop_threads_for_row` | Firestore |
| Follow-up state | `cancel_followup_on_response`, `schedule_followup_after_auto_response`, direct follow-up patches | Firestore and scheduler-visible state |
| Human review | `write_notification`, action-needed records, event-handled markers | Firestore and dashboard |
| Retry and recovery | `queue_pending_response`, dead-letter moves, reconciliation records, pending-response sends | Firestore, Microsoft Graph |
| Contact suppression | `_store_contact_optout` and terminal thread mutations | Firestore |
| Campaign completion | `_maybe_mark_client_completed` and row-completion notifications | Firestore |

The adapter is not a wrapper around any of these functions. Later service
adapters may consume its eligibility receipts, but only after separate staging,
authorization, persistence, and effect-specific proof gates.

## Approaches Considered

### 1. Pure eligibility evaluator (selected)

Create a small module inside `email_automation.claim_pipeline` that accepts an
already validated action plan plus a sanitized current-state context and emits
deterministic dry-run receipts. It has no callback, protocol, client, repository,
transport, or service dependency.

This is the smallest design that proves the safety contract independently of
the legacy worker. Static import isolation can prove that no effect is reachable.

### 2. Generic executor protocol with fake drivers (rejected for this gate)

A protocol such as `EffectDriver.apply(action)` would resemble the eventual
runtime design, but even a fake driver expands this experiment into effect
orchestration. Tests could accidentally validate the fake rather than the
safety gate, and a real driver could later be injected without changing the
boundary. That is too much authority for the current phase.

### 3. Legacy worker dry-run flag (rejected)

Adding `dry_run=True` to `process_inbox_message` or the existing mutation
helpers would preserve all production imports and depend on every branch
honoring the flag. The legacy function already contains direct service calls,
late imports, retries, and nested helpers. A missed branch would create a real
effect, so this approach cannot provide structural isolation.

## Proposed Architecture

### Module Boundary

Create `email_automation/claim_pipeline/effect_adapter.py` with pure immutable
contracts and one public evaluator:

```python
evaluate_effect_plan(request: EffectAdapterRequest) -> DryRunCommitReceipt
```

The module may import only the Python standard library and
`email_automation.claim_pipeline` modules. It must expose no callback or object
that can perform I/O.

Fixture loading and report generation remain separate pure modules so the core
evaluator does not know about files:

```text
effect_adapter.py              contracts, reason lattice, pure evaluation
effect_adapter_fixtures.py     sanitized JSON fixture parser and exact oracle
run_claim_pipeline_effect_adapter_dry_run.py
                               clean-tree deterministic report runner
```

### Input Contracts

`ApprovalGrant`

- `grant_id`: stable hash of all grant fields.
- `tenant_id`, `plan_id`, `action_id`, `snapshot_hash`: exact ownership scope.
- `approved_by`: opaque fixture/operator identity; never an email address.
- `approval_class`: must be `human_required`.
- An approval for another action, plan, tenant, or snapshot is invalid.

`EffectAdapterRequest`

- `plan`: the immutable `ActionPlan` to evaluate.
- `decision`: the exact `DecisionSnapshot` referenced by the plan.
- `scope`: the exact `ExecutionScope` used to construct the actions.
- `entities` and `claims`: the provenance bundle required to rerun
  `validate_action_plan` at the adapter boundary.
- `authorized_recipients`: sanitized recipient allowlist used by the plan
  validator.
- `current_snapshot_hash`, `current_contract_id`,
  `current_contract_version`: current persistence snapshot identity supplied by
  a future read adapter. All must exactly match the plan and decision.
- `current_state_by_action_id`: an exact prior-state mapping for each planned
  action. The supplied value must equal `action.expected_prior_state`.
- `approval_grants`: exact grants for human-required actions.
- `committed_idempotency_keys`: semantic effects already committed by an
  external system. This is data only; the evaluator never reads the store.

The request constructor freezes all collections and rejects duplicate action
state entries, approval grants, and idempotency keys.

### Output Contracts

`DryRunStatus`

- `would_apply`: all pure gates passed. This is eligibility only.
- `blocked`: the action is unsafe or invalid under current evidence.
- `skipped`: the action is valid but deliberately not eligible, such as an
  already committed effect or a human action awaiting approval.

The word `applied` is forbidden as a dry-run status.

`DryRunReason`

Use a closed reason vocabulary:

- `eligible_automatic_action`
- `eligible_human_approved_action`
- `approval_required`
- `approval_scope_mismatch`
- `unsupported_action_type`
- `stale_snapshot`
- `stale_contract`
- `prior_state_mismatch`
- `idempotency_key_already_committed`
- `dependency_blocked`
- `terminal_outbound_suppressed`
- `plan_contract_violation`

`DryRunEffectReceipt`

- Stable `receipt_id` hashed from plan ID, action ID, idempotency key, status,
  reason, and dependency receipt IDs.
- `plan_id`, `action_id`, `idempotency_key`, `action_type`, `sequence`.
- `status` and exactly one closed `reason`.
- `dependency_receipt_ids` for auditable ordering.
- No raw payload, recipient, address, message body, customer identifier, error
  stack, external ID, or timestamp.

`DryRunCommitReceipt`

- Stable `receipt_id` over the request identity and ordered effect receipt IDs.
- Tenant, plan, decision, contract version, and snapshot identities.
- Ordered `effects` containing exactly one receipt per planned action.
- Aggregate counts for `would_apply`, `blocked`, and `skipped` derived from the
  receipts, never supplied independently.
- No `completed_at`; wall-clock time would make repeated reports differ.

### Supported Action Surface

The first adapter gate recognizes every `ActionType` but deliberately grants
eligibility to only the currently planned policy actions:

| Action | Expected classification |
|---|---|
| `fact_update` | Automatic; `would_apply` only with exact current prior state |
| `followup_freeze` | Automatic; `would_apply` only with exact current prior state and terminal decision |
| `status_transition` | Automatic; `would_apply` only with exact current prior state |
| `alternate_property_proposal` | Human-required; skipped without exact approval, otherwise `would_apply` |
| `recipient_change` | Human-required; skipped without exact approval, otherwise `would_apply` |
| `call_request` | Human-required; skipped without exact approval, otherwise `would_apply` |
| `tour_request` | Human-required; skipped without exact approval, otherwise `would_apply` |
| `information_request` | Human-required; skipped without exact approval, otherwise `would_apply` |
| `review_item` | Human-required; skipped without exact approval, otherwise `would_apply` |
| All other action types | `blocked` with `unsupported_action_type` |

An action carrying `ApprovalClass.FORBIDDEN` is not a valid plan. The existing
`validate_action_plan` boundary rejects it before action-level evaluation, so
every action in that plan receives `blocked:plan_contract_violation`.

`outbound_draft` is always blocked in this phase. If the decision or current
conversation is terminal, it receives the more specific
`terminal_outbound_suppressed` reason. This design does not draft or send an
email under any status.

### Evaluation Order

The evaluator is deterministic and fail closed in this order:

1. Rerun `validate_action_plan` against the exact decision, scope, entities,
   claims, and recipient authorization. A violation blocks every action with
   `plan_contract_violation`; it never raises after accepting a well-formed
   `EffectAdapterRequest`.
2. Compare current snapshot and contract identity to the plan and decision.
   Any mismatch blocks every action as stale.
3. Process actions by `(sequence, action_id)` and require every dependency to
   refer to a preceding `would_apply` receipt.
4. Reject unsupported action types.
5. Suppress outbound actions under terminal state before checking approval.
6. Skip any idempotency key already present in committed history.
7. Compare the exact supplied current state with `expected_prior_state`.
8. For human-required actions, require one exact approval grant. Missing grants
   skip; wrong-scope grants block.
9. Emit `would_apply` only after every applicable gate passes.

The evaluator does not mutate its in-memory committed-key set as it walks the
plan. Duplicate semantic actions inside one plan are already rejected by
`validate_action_plan`; committed history represents only effects from earlier
external attempts.

## Deterministic Fixture Lattice

The sanitized fixture catalog must include exact opposed cases, not one-off
examples tied to Jill or Baylor:

1. Automatic fact update with matching prior state.
2. Automatic fact update with stale value.
3. Whole-plan stale snapshot.
4. Whole-plan stale contract version and identity.
5. Previously committed idempotency key.
6. Human action without approval.
7. Human action with exact approval.
8. Approval for the wrong action.
9. Approval for the wrong plan or snapshot.
10. Forbidden action rejected as a whole-plan contract violation.
11. Unsupported notification, row-move, note, LOI, and outbound-draft actions.
12. Terminal decision with outbound draft.
13. Follow-up freeze with terminal evidence and matching state.
14. Dependency chain where all predecessors are eligible.
15. Dependency chain blocked by an ineligible predecessor.
16. Duplicate dependency and reversed sequence construction failures.
17. Wrong tenant, campaign, entity, row, thread, Sheet, claim, or recipient
    binding rejected by the reused plan validator.
18. Repeated and reversed-input runs produce identical receipt IDs and report
    digests.

Every case declares exact expected status/reason pairs by action signature.
The runner fails on any missing, extra, or differently classified receipt.

## Isolation and Privacy Proof

The existing package import-isolation test will continue to allow only standard
library modules and `email_automation.claim_pipeline`. A new negative fixture
must prove the scanner resolves relative imports such as `..processing`,
`..pending_responses`, and `..sheets` as forbidden.

Additional static checks must reject these tokens from the adapter and fixture
modules:

- `requests`, `google`, `firebase`, `firestore`, `graph`, `sheets`, `msal`
- `processing`, `pending_responses`, `followup`, `notifications`, `outbox`
- callable fields, protocols, clients, repositories, transports, or hooks

The report privacy scan rejects email-like strings, fixture street addresses,
raw claim evidence, recipients, message bodies, action payloads, and exception
stacks. Reports contain only stable IDs, action types, statuses, reasons, counts,
and hashes.

## Error Handling

- Constructor/schema failures raise before evaluation and produce no receipt.
- A valid request containing a plan-level contract violation returns a complete
  blocked receipt set with the closed `plan_contract_violation` reason.
- Action-level failures never disappear: every planned action receives exactly
  one receipt.
- No broad exception is translated to success. Unexpected exceptions fail the
  runner and prevent an evidence report from being marked passed.
- There are no retries because no external operation exists.

## Verification Gates

The implementation is acceptable only when all of the following pass:

1. Focused contract, evaluator, fixture, report, and isolation tests.
2. A clean-tree runner repeated at least three times with one result digest.
3. Exact fixture oracle: zero missing, extra, or mismatched receipts.
4. Static import and callable-surface isolation.
5. Privacy scan over source fixtures and generated report.
6. Existing claim-policy tests remain green.
7. Full backend suite remains green.
8. `git diff --check` and compilation pass.
9. Evidence document records source revision, source-tree hash, fixture hash,
   result digest, counts, tests, and explicit no-effect scope.

Passing this gate unlocks design of a separate disabled staging persistence
adapter. It does not unlock a real service adapter, production integration,
browser campaign execution, deployment, or any send.

## Explicit Non-Goals

- No production or staging persistence.
- No generic effect-driver protocol.
- No changes to `process_inbox_message` or any legacy handler.
- No Graph, Firestore, Sheets, mailbox, queue, scheduler, notification, draft,
  reply, follow-up, or deployment access.
- No automatic redirect behavior.
- No provider calls.
- No customer or live campaign data.
- No claim that an eligible dry-run action has been applied.

## Decision

Proceed with the pure eligibility evaluator after design approval. Keep the
legacy worker and every external effect disconnected. The build must start with
opposed failing tests for receipt vocabulary, stale state, approval ownership,
idempotency, dependencies, and terminal outbound suppression before adding the
evaluator implementation.
