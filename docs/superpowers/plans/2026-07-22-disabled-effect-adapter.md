# Disabled Effect Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure, deterministic dry-run eligibility adapter that proves whether validated actions would be safe to apply without importing, receiving, or invoking any production effect surface.

**Architecture:** Add immutable dry-run contracts and a pure evaluator inside `email_automation.claim_pipeline`. The evaluator reruns the existing action-plan validator, checks current snapshot and contract identity, dependencies, supported action type, terminal suppression, idempotency history, exact prior state, and exact human approval, then emits privacy-safe `would_apply`, `blocked`, or `skipped` receipts. A sanitized fixture lattice and clean-tree runner prove deterministic behavior and structural isolation; the legacy worker remains disconnected.

**Tech Stack:** Python 3.11, frozen dataclasses, enums, SHA-256 canonical JSON identities, `unittest`, AST import inspection, JSON fixtures, existing SiteSift claim-policy contracts.

---

## Approved Scope

Deliverable: **code**. The prior design finding is already committed at `26f76d0`.

This plan creates no persistence or service adapter. It must not modify or import
`processing.py`, `ai_processing.py`, `pending_responses.py`, Graph, Firestore,
Google Sheets, follow-up, notifications, queues, drafts, sends, scheduler, or
deployment code. It uses no provider and no live/customer data.

## File Structure

| File | Responsibility |
|---|---|
| `email_automation/claim_pipeline/effect_adapter.py` | Immutable request/grant/receipt contracts, closed status/reason vocabulary, pure gate evaluator |
| `email_automation/claim_pipeline/effect_adapter_fixtures.py` | Strict parser and builder for sanitized adapter cases; exact expected-oracle comparison |
| `email_automation/claim_pipeline/__init__.py` | Export only the new pure public API |
| `tests/fixtures/claim_pipeline_effect_adapter_cases.json` | Eighteen opposed, sanitized effect-boundary cases |
| `tests/test_claim_pipeline_effect_adapter.py` | Contract and evaluator red/green tests |
| `tests/test_claim_pipeline_effect_adapter_fixtures.py` | Fixture schema, coverage, and exact-oracle tests |
| `tests/test_claim_pipeline_effect_adapter_report.py` | Runner identity, determinism, privacy, and clean-tree tests |
| `tests/test_claim_pipeline_isolation.py` | Explicitly reject service imports and callable injection surfaces |
| `scripts/run_claim_pipeline_effect_adapter_dry_run.py` | No-provider, clean-tree deterministic report runner |
| `docs/release-safety/disabled-effect-adapter-evidence-2026-07-22.md` | Final evidence and honest unlock/remaining-lock statement |

## Task 1: Immutable Dry-Run Contracts

**Files:**
- Create: `email_automation/claim_pipeline/effect_adapter.py`
- Create: `tests/test_claim_pipeline_effect_adapter.py`

- [ ] **Step 1: Write failing tests for the vocabulary and immutable identities**

Create the first test class with these exact public imports and assertions:

```python
import json
import unittest
from dataclasses import replace

from email_automation.claim_pipeline.effect_adapter import (
    ActionStateSnapshot,
    ApprovalGrant,
    DryRunCommitReceipt,
    DryRunEffectReceipt,
    DryRunReason,
    DryRunStatus,
    EffectAdapterRequest,
)


class EffectAdapterContractTests(unittest.TestCase):
    def test_dry_run_status_cannot_report_applied(self):
        self.assertEqual(
            {"would_apply", "blocked", "skipped"},
            {status.value for status in DryRunStatus},
        )
        self.assertNotIn("applied", {status.value for status in DryRunStatus})

    def test_approval_grant_identity_rejects_tampering(self):
        grant = ApprovalGrant.create(
            tenant_id="tenant-fixture",
            plan_id="plan-fixture",
            action_id="action-fixture",
            snapshot_hash="snapshot-fixture",
            approved_by="operator-fixture",
        )
        with self.assertRaisesRegex(ValueError, "grant identity"):
            replace(grant, plan_id="different-plan")

    def test_request_rejects_duplicate_action_state_and_history(self):
        state = ActionStateSnapshot.create(
            action_id="action-fixture",
            values={"conversationState": "active"},
        )
        common = _minimal_request_fields()
        with self.assertRaisesRegex(ValueError, "duplicate action state"):
            EffectAdapterRequest.create(
                current_states=(state, state),
                committed_idempotency_keys=(),
                approval_grants=(),
                **common,
            )
        with self.assertRaisesRegex(ValueError, "duplicate committed idempotency"):
            EffectAdapterRequest.create(
                current_states=(state,),
                committed_idempotency_keys=("effect-fixture", "effect-fixture"),
                approval_grants=(),
                **common,
            )

    def test_receipts_are_stable_and_exclude_effect_payloads(self):
        effect = DryRunEffectReceipt.create(
            plan_id="plan-fixture",
            action_id="action-fixture",
            idempotency_key="effect-fixture",
            action_type="fact_update",
            sequence=1,
            status=DryRunStatus.WOULD_APPLY,
            reason=DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
            dependency_receipt_ids=(),
        )
        commit = DryRunCommitReceipt.create(
            tenant_id="tenant-fixture",
            plan_id="plan-fixture",
            decision_id="decision-fixture",
            contract_id="contract-fixture",
            contract_version=1,
            snapshot_hash="snapshot-fixture",
            effects=(effect,),
        )
        encoded = json.dumps(commit.to_dict(), sort_keys=True)
        self.assertEqual(1, commit.status_counts["would_apply"])
        self.assertNotIn("payload", encoded)
        self.assertNotIn("recipient", encoded)
        self.assertNotIn("external", encoded)
        self.assertNotIn("completedAt", encoded)


if __name__ == "__main__":
    unittest.main()
```

Define `_minimal_request_fields()` in the same test file by constructing one
valid synthetic `CampaignContract`, `ExecutionScope`, `EntityRef`, `Claim`,
`DecisionSnapshot`, `PlannedAction`, and `ActionPlan` through their existing
`.create()` factories. Use only opaque fixture tokens such as
`tenant-fixture`, `campaign-fixture`, `thread-fixture`, `sheet-fixture`, and
`row-fixture`; do not use an email address or street address.

- [ ] **Step 2: Run the contract tests and verify RED**

Run:

```bash
.venv/bin/python -m unittest tests.test_claim_pipeline_effect_adapter.EffectAdapterContractTests -v
```

Expected: import failure because `effect_adapter.py` does not exist.

- [ ] **Step 3: Add the exact closed vocabularies and immutable contracts**

Implement these public types in `effect_adapter.py`:

```python
class DryRunStatus(str, Enum):
    WOULD_APPLY = "would_apply"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class DryRunReason(str, Enum):
    ELIGIBLE_AUTOMATIC_ACTION = "eligible_automatic_action"
    ELIGIBLE_HUMAN_APPROVED_ACTION = "eligible_human_approved_action"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_SCOPE_MISMATCH = "approval_scope_mismatch"
    UNSUPPORTED_ACTION_TYPE = "unsupported_action_type"
    STALE_SNAPSHOT = "stale_snapshot"
    STALE_CONTRACT = "stale_contract"
    PRIOR_STATE_MISMATCH = "prior_state_mismatch"
    IDEMPOTENCY_KEY_ALREADY_COMMITTED = "idempotency_key_already_committed"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    TERMINAL_OUTBOUND_SUPPRESSED = "terminal_outbound_suppressed"
    PLAN_CONTRACT_VIOLATION = "plan_contract_violation"
```

Add frozen dataclasses with `.create()` identity factories and `.to_dict()`:

```python
@dataclass(frozen=True)
class ActionStateSnapshot:
    action_id: str
    state_id: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class ApprovalGrant:
    tenant_id: str
    plan_id: str
    action_id: str
    snapshot_hash: str
    approved_by: str
    grant_id: str


@dataclass(frozen=True)
class EffectAdapterRequest:
    plan: ActionPlan
    decision: DecisionSnapshot
    scope: ExecutionScope
    entities: tuple[EntityRef, ...]
    claims: tuple[Claim, ...]
    authorized_recipients: tuple[str, ...]
    current_snapshot_hash: str
    current_contract_id: str
    current_contract_version: int
    current_states: tuple[ActionStateSnapshot, ...]
    approval_grants: tuple[ApprovalGrant, ...]
    committed_idempotency_keys: tuple[str, ...]


@dataclass(frozen=True)
class DryRunEffectReceipt:
    receipt_id: str
    plan_id: str
    action_id: str
    idempotency_key: str
    action_type: str
    sequence: int
    status: DryRunStatus
    reason: DryRunReason
    dependency_receipt_ids: tuple[str, ...]


@dataclass(frozen=True)
class DryRunCommitReceipt:
    receipt_id: str
    tenant_id: str
    plan_id: str
    decision_id: str
    contract_id: str
    contract_version: int
    snapshot_hash: str
    effects: tuple[DryRunEffectReceipt, ...]

    @property
    def status_counts(self) -> Mapping[str, int]:
        return MappingProxyType(
            {
                status.value: sum(effect.status is status for effect in self.effects)
                for status in DryRunStatus
            }
        )
```

Use local `_freeze_json`, `_json_ready`, and `_stable_id` helpers with
`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)` and
SHA-256 truncated to 24 hex characters, matching the existing contract style.
Every direct constructor validates its stable identity in `__post_init__`.
`EffectAdapterRequest.create()` freezes sequences, rejects duplicate state
action IDs, duplicate grant IDs, and duplicate committed keys, and requires one
`ActionStateSnapshot` for every action in the plan.

- [ ] **Step 4: Run the contract tests and verify GREEN**

Run the command from Step 2. Expected: all contract tests pass.

- [ ] **Step 5: Commit the contract slice**

```bash
git add email_automation/claim_pipeline/effect_adapter.py tests/test_claim_pipeline_effect_adapter.py
git commit -m "feat: add immutable dry-run effect contracts"
```

## Task 2: Fail-Closed Eligibility Evaluator

**Files:**
- Modify: `email_automation/claim_pipeline/effect_adapter.py`
- Modify: `tests/test_claim_pipeline_effect_adapter.py`

- [ ] **Step 1: Add opposed evaluator tests before implementation**

Add an `EffectAdapterEvaluationTests` class. Give the test factory this exact
signature; it must construct the requested action through `PlannedAction.create`
and reconstruct the containing `ActionPlan` through `ActionPlan.create` so every
non-malformed case retains valid derived identities:

```python
def _request_fixture(
    *,
    action_type: ActionType = ActionType.FACT_UPDATE,
    approval_class: ApprovalClass = ApprovalClass.AUTOMATIC,
    conversation_state: ConversationState = ConversationState.ACTIVE,
    current_snapshot_hash: str | None = None,
    current_contract_id: str | None = None,
    current_contract_version: int | None = None,
    current_values: dict | None = None,
    committed_idempotency_keys: tuple[str, ...] = (),
    approval_grants: tuple[ApprovalGrant, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> EffectAdapterRequest:
```

The default factory uses one synthetic availability claim and the exact action
inputs below. For multi-action helpers, create one matching claim per action.

| Action type | Source predicate | Payload | Expected/current prior state | Recipient |
|---|---|---|---|---|
| `fact_update` | `availability` | `{"field": "availability", "value": "available", "confidence": 0.99}` | `{"availability": "unknown"}` | empty |
| `followup_freeze` | `opt_out` | `{"reason": "contact_opt_out"}` | `{"followUpStatus": "waiting"}` | empty |
| `status_transition` | `availability` | `{"status": "waiting_user"}` | `{"conversationState": "active"}` | empty |
| `alternate_property_proposal` | `identity` | `{"summary": "alternate-fixture"}` | `{}` | empty |
| `recipient_change` | `referral` | `{"reason": "redirect_requires_approval"}` | `{"recipient": ""}` | `recipient-fixture` |
| `call_request` | `call_request` | `{"notes": "call-fixture", "phone": ""}` | `{}` | empty |
| `tour_request` | `tour_request` | `{"notes": "tour-fixture"}` | `{}` | empty |
| `information_request` | `information_request` | `{"notes": "information-fixture"}` | `{}` | empty |
| `review_item` | `availability` | `{"summary": "review-fixture", "details": {"reasonCodes": ["fixture-review"]}}` | `{}` | empty |
| `note_append` | `availability` | `{"text": "note-fixture"}` | `{"note": ""}` | empty |
| `row_move` | `availability` | `{"destination": "nonviable-fixture"}` | `{"rowState": "active"}` | empty |
| `notification` | `availability` | `{"message": "notification-fixture"}` | `{}` | empty |
| `loi_request` | `availability` | `{"notes": "loi-fixture", "terms": {}}` | `{}` | empty |
| `outbound_draft` | `availability` | `{"subject": "subject-fixture", "body": "body-fixture"}` | `{}` | `recipient-fixture` |

For `followup_freeze`, construct a terminal-intent decision whose reason codes
contain `contact_opt_out`. For `alternate_property_proposal`, construct an
alternate entity. The referral claim value is
`{"name": "recipient-fixture", "email": "recipient-fixture"}`. For
`review_item`, construct a review decision. These adjustments are part of the
factory's action-type switch and occur before action/plan identities are built.

The test methods and exact expected pairs are:

```python
def test_matching_automatic_action_would_apply(self):
    receipt = evaluate_effect_plan(_request_fixture()).effects[0]
    self.assertEqual(
        (DryRunStatus.WOULD_APPLY, DryRunReason.ELIGIBLE_AUTOMATIC_ACTION),
        (receipt.status, receipt.reason),
    )

def test_stale_snapshot_blocks_every_action(self):
    receipt = evaluate_effect_plan(
        _request_fixture(current_snapshot_hash="stale-snapshot")
    ).effects[0]
    self.assertEqual(DryRunReason.STALE_SNAPSHOT, receipt.reason)

def test_stale_contract_blocks_every_action(self):
    receipt = evaluate_effect_plan(
        _request_fixture(current_contract_version=2)
    ).effects[0]
    self.assertEqual(DryRunReason.STALE_CONTRACT, receipt.reason)

def test_prior_state_mismatch_blocks_action(self):
    receipt = evaluate_effect_plan(
        _request_fixture(current_values={"availability": "different"})
    ).effects[0]
    self.assertEqual(DryRunReason.PRIOR_STATE_MISMATCH, receipt.reason)

def test_committed_idempotency_key_skips_action(self):
    request = _request_fixture()
    request = replace(
        request,
        committed_idempotency_keys=(request.plan.actions[0].idempotency_key,),
    )
    receipt = evaluate_effect_plan(request).effects[0]
    self.assertEqual(DryRunReason.IDEMPOTENCY_KEY_ALREADY_COMMITTED, receipt.reason)

def test_human_action_without_approval_is_skipped(self):
    receipt = evaluate_effect_plan(
        _request_fixture(
            action_type=ActionType.INFORMATION_REQUEST,
            approval_class=ApprovalClass.HUMAN_REQUIRED,
        )
    ).effects[0]
    self.assertEqual(
        (DryRunStatus.SKIPPED, DryRunReason.APPROVAL_REQUIRED),
        (receipt.status, receipt.reason),
    )

def test_exact_human_approval_would_apply(self):
    request = _request_fixture(
        action_type=ActionType.INFORMATION_REQUEST,
        approval_class=ApprovalClass.HUMAN_REQUIRED,
    )
    request = _with_exact_approval(request)
    receipt = evaluate_effect_plan(request).effects[0]
    self.assertEqual(DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION, receipt.reason)

def test_every_supported_human_action_requires_then_accepts_exact_approval(self):
    human_types = (
        ActionType.ALTERNATE_PROPERTY_PROPOSAL,
        ActionType.RECIPIENT_CHANGE,
        ActionType.CALL_REQUEST,
        ActionType.TOUR_REQUEST,
        ActionType.INFORMATION_REQUEST,
        ActionType.REVIEW_ITEM,
    )
    for action_type in human_types:
        with self.subTest(action_type=action_type.value):
            request = _request_fixture(
                action_type=action_type,
                approval_class=ApprovalClass.HUMAN_REQUIRED,
            )
            self.assertEqual(
                DryRunReason.APPROVAL_REQUIRED,
                evaluate_effect_plan(request).effects[0].reason,
            )
            self.assertEqual(
                DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION,
                evaluate_effect_plan(_with_exact_approval(request)).effects[0].reason,
            )

def test_wrong_scope_approval_blocks_action(self):
    request = _with_exact_approval(
        _request_fixture(
            action_type=ActionType.INFORMATION_REQUEST,
            approval_class=ApprovalClass.HUMAN_REQUIRED,
        )
    )
    wrong = ApprovalGrant.create(
        tenant_id=request.plan.tenant_id,
        plan_id="wrong-plan",
        action_id=request.plan.actions[0].action_id,
        snapshot_hash=request.current_snapshot_hash,
        approved_by="operator-fixture",
    )
    request = replace(request, approval_grants=(wrong,))
    receipt = evaluate_effect_plan(request).effects[0]
    self.assertEqual(DryRunReason.APPROVAL_SCOPE_MISMATCH, receipt.reason)

def test_terminal_outbound_draft_is_suppressed(self):
    receipt = evaluate_effect_plan(
        _request_fixture(
            action_type=ActionType.OUTBOUND_DRAFT,
            approval_class=ApprovalClass.HUMAN_REQUIRED,
            conversation_state=ConversationState.TERMINAL_INTENT,
        )
    ).effects[0]
    self.assertEqual(DryRunReason.TERMINAL_OUTBOUND_SUPPRESSED, receipt.reason)

def test_unsupported_action_is_blocked(self):
    receipt = evaluate_effect_plan(
        _request_fixture(action_type=ActionType.NOTIFICATION)
    ).effects[0]
    self.assertEqual(DryRunReason.UNSUPPORTED_ACTION_TYPE, receipt.reason)

def test_blocked_dependency_blocks_dependent_action(self):
    receipt = evaluate_effect_plan(
        _two_action_dependency_request(first_state_matches=False)
    )
    self.assertEqual(DryRunReason.PRIOR_STATE_MISMATCH, receipt.effects[0].reason)
    self.assertEqual(DryRunReason.DEPENDENCY_BLOCKED, receipt.effects[1].reason)

def test_forbidden_action_blocks_the_whole_plan_as_contract_violation(self):
    receipt = evaluate_effect_plan(
        _request_fixture(approval_class=ApprovalClass.FORBIDDEN)
    ).effects[0]
    self.assertEqual(DryRunReason.PLAN_CONTRACT_VIOLATION, receipt.reason)

def test_repeated_and_reversed_inputs_are_byte_stable(self):
    request = _two_action_dependency_request(first_state_matches=True)
    forward = evaluate_effect_plan(request)
    repeated = evaluate_effect_plan(request)
    reversed_request = replace(
        request,
        current_states=tuple(reversed(request.current_states)),
        approval_grants=tuple(reversed(request.approval_grants)),
        committed_idempotency_keys=tuple(reversed(request.committed_idempotency_keys)),
    )
    reversed_result = evaluate_effect_plan(reversed_request)
    self.assertEqual(forward.receipt_id, repeated.receipt_id)
    self.assertEqual(forward.receipt_id, reversed_result.receipt_id)
```

- [ ] **Step 2: Run the evaluator tests and verify RED**

```bash
.venv/bin/python -m unittest tests.test_claim_pipeline_effect_adapter.EffectAdapterEvaluationTests -v
```

Expected: failure because `evaluate_effect_plan` is absent.

- [ ] **Step 3: Implement the evaluator in the approved gate order**

Add these exact action sets:

```python
SUPPORTED_ACTION_TYPES = frozenset(
    {
        ActionType.FACT_UPDATE,
        ActionType.FOLLOWUP_FREEZE,
        ActionType.STATUS_TRANSITION,
        ActionType.ALTERNATE_PROPERTY_PROPOSAL,
        ActionType.RECIPIENT_CHANGE,
        ActionType.CALL_REQUEST,
        ActionType.TOUR_REQUEST,
        ActionType.INFORMATION_REQUEST,
        ActionType.REVIEW_ITEM,
    }
)
OUTBOUND_ACTION_TYPES = frozenset({ActionType.OUTBOUND_DRAFT})
TERMINAL_STATES = frozenset(
    {
        ConversationState.TERMINAL_INTENT,
        ConversationState.TERMINAL_PENDING_ACK,
        ConversationState.TERMINAL,
    }
)
```

Implement `evaluate_effect_plan()` as one deterministic pass. Use a private
`_receipt(action, status, reason, dependencies)` helper so every branch emits
the same privacy-safe shape:

```python
def evaluate_effect_plan(request: EffectAdapterRequest) -> DryRunCommitReceipt:
    ordered = tuple(sorted(request.plan.actions, key=lambda item: (item.sequence, item.action_id)))
    try:
        validate_action_plan(
            request.plan,
            request.decision,
            scope=request.scope,
            entities=request.entities,
            claims=request.claims,
            authorized_recipients=request.authorized_recipients,
        )
    except ContractViolation:
        return _commit(
            request,
            tuple(
                _receipt(
                    request.plan.plan_id,
                    action,
                    DryRunStatus.BLOCKED,
                    DryRunReason.PLAN_CONTRACT_VIOLATION,
                    (),
                )
                for action in ordered
            ),
        )

    global_reason = _request_identity_failure(request)
    if global_reason is not None:
        return _commit(
            request,
            tuple(
                _receipt(
                    request.plan.plan_id,
                    action,
                    DryRunStatus.BLOCKED,
                    global_reason,
                    (),
                )
                for action in ordered
            ),
        )

    states = {item.action_id: item for item in request.current_states}
    grants = _grants_by_action(request.approval_grants)
    committed = frozenset(request.committed_idempotency_keys)
    receipts_by_action = {}
    receipts = []

    for action in ordered:
        dependency_receipts = tuple(
            receipts_by_action[dependency_id] for dependency_id in action.dependencies
        )
        if any(item.status is not DryRunStatus.WOULD_APPLY for item in dependency_receipts):
            status, reason = DryRunStatus.BLOCKED, DryRunReason.DEPENDENCY_BLOCKED
        elif action.action_type not in SUPPORTED_ACTION_TYPES:
            if (
                action.action_type in OUTBOUND_ACTION_TYPES
                and request.decision.conversation_state in TERMINAL_STATES
            ):
                status, reason = (
                    DryRunStatus.BLOCKED,
                    DryRunReason.TERMINAL_OUTBOUND_SUPPRESSED,
                )
            else:
                status, reason = DryRunStatus.BLOCKED, DryRunReason.UNSUPPORTED_ACTION_TYPE
        elif action.idempotency_key in committed:
            status, reason = (
                DryRunStatus.SKIPPED,
                DryRunReason.IDEMPOTENCY_KEY_ALREADY_COMMITTED,
            )
        elif states[action.action_id].values != action.expected_prior_state:
            status, reason = DryRunStatus.BLOCKED, DryRunReason.PRIOR_STATE_MISMATCH
        elif action.approval_class is ApprovalClass.HUMAN_REQUIRED:
            status, reason = _human_approval_disposition(request, action, grants)
        else:
            status, reason = (
                DryRunStatus.WOULD_APPLY,
                DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
            )

        receipt = _receipt(
            request.plan.plan_id,
            action,
            status,
            reason,
            tuple(item.receipt_id for item in dependency_receipts),
        )
        receipts.append(receipt)
        receipts_by_action[action.action_id] = receipt

    return _commit(request, tuple(receipts))
```

`_request_identity_failure()` returns `STALE_SNAPSHOT` if the current snapshot
does not equal both plan and decision; otherwise it returns `STALE_CONTRACT` if
contract ID/version do not equal both plan and decision. `_human_approval_disposition()`
returns `SKIPPED/APPROVAL_REQUIRED` when no grant has the action ID, returns
`BLOCKED/APPROVAL_SCOPE_MISMATCH` when any ownership field differs, and returns
`WOULD_APPLY/ELIGIBLE_HUMAN_APPROVED_ACTION` for exactly one matching grant.

- [ ] **Step 4: Run evaluator and existing policy tests**

```bash
.venv/bin/python -m unittest \
  tests.test_claim_pipeline_effect_adapter \
  tests.test_claim_pipeline_contracts \
  tests.test_claim_pipeline_policy \
  -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the evaluator slice**

```bash
git add email_automation/claim_pipeline/effect_adapter.py tests/test_claim_pipeline_effect_adapter.py
git commit -m "feat: evaluate dry-run effect eligibility"
```

## Task 3: Sanitized Eighteen-Case Fixture Lattice

**Files:**
- Create: `email_automation/claim_pipeline/effect_adapter_fixtures.py`
- Create: `tests/fixtures/claim_pipeline_effect_adapter_cases.json`
- Create: `tests/test_claim_pipeline_effect_adapter_fixtures.py`

- [ ] **Step 1: Write failing fixture-schema and exact-oracle tests**

Create tests that require schema version
`claim-pipeline-effect-adapter-fixtures-v1`, exactly 18 unique case IDs, all
three statuses, every closed reason reachable under the approved surface, no
email-like strings, and these required case IDs:

```python
REQUIRED_CASE_IDS = {
    "automatic-fact-matching",
    "automatic-fact-stale-prior",
    "whole-plan-stale-snapshot",
    "whole-plan-stale-contract",
    "already-committed-effect",
    "human-action-no-approval",
    "human-action-exact-approval",
    "approval-for-other-action",
    "approval-wrong-plan",
    "forbidden-plan",
    "unsupported-actions",
    "terminal-outbound-draft",
    "terminal-followup-freeze",
    "dependency-chain-eligible",
    "dependency-chain-blocked",
    "dependency-construction-rejected",
    "scope-and-provenance-rejected",
    "input-order-byte-stable",
}
```

For each loaded case, call `run_effect_adapter_fixture_case(case)` and compare
the complete ordered tuple of `(action_signature, status, reason)` against the
fixture's `expectedReceipts`. Assert the report contains exactly one receipt
per planned action.

- [ ] **Step 2: Run fixture tests and verify RED**

```bash
.venv/bin/python -m unittest tests.test_claim_pipeline_effect_adapter_fixtures -v
```

Expected: import failure because the fixture loader is absent.

- [ ] **Step 3: Add the strict JSON fixture catalog**

Use this complete catalog shape. The fixture parser rejects mutation/action
tokens outside the values shown here:

```json
{
  "schemaVersion": "claim-pipeline-effect-adapter-fixtures-v1",
  "cases": [
    {
      "caseId": "automatic-fact-matching",
      "actions": [{"type": "fact_update", "approval": "automatic"}],
      "mutations": [],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "would_apply", "reason": "eligible_automatic_action"}
      ]
    },
    {
      "caseId": "automatic-fact-stale-prior",
      "actions": [{"type": "fact_update", "approval": "automatic"}],
      "mutations": ["stale_prior_state:1"],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "blocked", "reason": "prior_state_mismatch"}
      ]
    },
    {
      "caseId": "whole-plan-stale-snapshot",
      "actions": [{"type": "fact_update", "approval": "automatic"}],
      "mutations": ["stale_snapshot"],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "blocked", "reason": "stale_snapshot"}
      ]
    },
    {
      "caseId": "whole-plan-stale-contract",
      "actions": [{"type": "fact_update", "approval": "automatic"}],
      "mutations": ["stale_contract"],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "blocked", "reason": "stale_contract"}
      ]
    },
    {
      "caseId": "already-committed-effect",
      "actions": [{"type": "fact_update", "approval": "automatic"}],
      "mutations": ["committed:1"],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "skipped", "reason": "idempotency_key_already_committed"}
      ]
    },
    {
      "caseId": "human-action-no-approval",
      "actions": [{"type": "information_request", "approval": "human_required"}],
      "mutations": [],
      "expectedReceipts": [
        {"action": "information_request:1", "status": "skipped", "reason": "approval_required"}
      ]
    },
    {
      "caseId": "human-action-exact-approval",
      "actions": [{"type": "information_request", "approval": "human_required"}],
      "mutations": ["approve:1"],
      "expectedReceipts": [
        {"action": "information_request:1", "status": "would_apply", "reason": "eligible_human_approved_action"}
      ]
    },
    {
      "caseId": "approval-for-other-action",
      "actions": [{"type": "information_request", "approval": "human_required"}],
      "mutations": ["approval_other_action:1"],
      "expectedReceipts": [
        {"action": "information_request:1", "status": "skipped", "reason": "approval_required"}
      ]
    },
    {
      "caseId": "approval-wrong-plan",
      "actions": [{"type": "information_request", "approval": "human_required"}],
      "mutations": ["approval_wrong_plan:1"],
      "expectedReceipts": [
        {"action": "information_request:1", "status": "blocked", "reason": "approval_scope_mismatch"}
      ]
    },
    {
      "caseId": "forbidden-plan",
      "actions": [{"type": "fact_update", "approval": "forbidden"}],
      "mutations": [],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "blocked", "reason": "plan_contract_violation"}
      ]
    },
    {
      "caseId": "unsupported-actions",
      "actions": [
        {"type": "note_append", "approval": "automatic"},
        {"type": "row_move", "approval": "automatic"},
        {"type": "notification", "approval": "automatic"},
        {"type": "loi_request", "approval": "human_required"},
        {"type": "outbound_draft", "approval": "human_required"}
      ],
      "mutations": [],
      "expectedReceipts": [
        {"action": "note_append:1", "status": "blocked", "reason": "unsupported_action_type"},
        {"action": "row_move:2", "status": "blocked", "reason": "unsupported_action_type"},
        {"action": "notification:3", "status": "blocked", "reason": "unsupported_action_type"},
        {"action": "loi_request:4", "status": "blocked", "reason": "unsupported_action_type"},
        {"action": "outbound_draft:5", "status": "blocked", "reason": "unsupported_action_type"}
      ]
    },
    {
      "caseId": "terminal-outbound-draft",
      "actions": [{"type": "outbound_draft", "approval": "human_required"}],
      "mutations": ["terminal_decision"],
      "expectedReceipts": [
        {"action": "outbound_draft:1", "status": "blocked", "reason": "terminal_outbound_suppressed"}
      ]
    },
    {
      "caseId": "terminal-followup-freeze",
      "actions": [{"type": "followup_freeze", "approval": "automatic"}],
      "mutations": ["terminal_decision"],
      "expectedReceipts": [
        {"action": "followup_freeze:1", "status": "would_apply", "reason": "eligible_automatic_action"}
      ]
    },
    {
      "caseId": "dependency-chain-eligible",
      "actions": [
        {"type": "fact_update", "approval": "automatic"},
        {"type": "status_transition", "approval": "automatic", "dependsOn": [1]}
      ],
      "mutations": [],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "would_apply", "reason": "eligible_automatic_action"},
        {"action": "status_transition:2", "status": "would_apply", "reason": "eligible_automatic_action"}
      ]
    },
    {
      "caseId": "dependency-chain-blocked",
      "actions": [
        {"type": "fact_update", "approval": "automatic"},
        {"type": "status_transition", "approval": "automatic", "dependsOn": [1]}
      ],
      "mutations": ["stale_prior_state:1"],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "blocked", "reason": "prior_state_mismatch"},
        {"action": "status_transition:2", "status": "blocked", "reason": "dependency_blocked"}
      ]
    },
    {
      "caseId": "dependency-construction-rejected",
      "actions": [
        {"type": "fact_update", "approval": "automatic", "dependsOn": [2]},
        {"type": "status_transition", "approval": "automatic"}
      ],
      "mutations": [],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "blocked", "reason": "plan_contract_violation"},
        {"action": "status_transition:2", "status": "blocked", "reason": "plan_contract_violation"}
      ]
    },
    {
      "caseId": "scope-and-provenance-rejected",
      "actions": [{"type": "fact_update", "approval": "automatic"}],
      "mutations": ["scope_row_mismatch"],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "blocked", "reason": "plan_contract_violation"}
      ]
    },
    {
      "caseId": "input-order-byte-stable",
      "actions": [
        {"type": "fact_update", "approval": "automatic"},
        {"type": "status_transition", "approval": "automatic", "dependsOn": [1]}
      ],
      "mutations": ["reverse_request_collections"],
      "expectedReceipts": [
        {"action": "fact_update:1", "status": "would_apply", "reason": "eligible_automatic_action"},
        {"action": "status_transition:2", "status": "would_apply", "reason": "eligible_automatic_action"}
      ]
    }
  ]
}
```

- [ ] **Step 4: Implement the fixture parser and builder**

Create frozen `EffectAdapterFixtureCase` and `EffectAdapterFixtureCatalog`
dataclasses plus:

```python
EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION = "claim-pipeline-effect-adapter-fixtures-v1"


def load_effect_adapter_fixture_catalog(path: Path) -> EffectAdapterFixtureCatalog:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schemaVersion") != EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION:
        raise EffectAdapterFixtureValidationError("unsupported fixture schema")
    cases = tuple(_parse_case(item) for item in raw.get("cases", ()))
    if len({case.case_id for case in cases}) != len(cases):
        raise EffectAdapterFixtureValidationError("duplicate fixture case ID")
    return EffectAdapterFixtureCatalog(schema_version=raw["schemaVersion"], cases=cases)


def run_effect_adapter_fixture_case(case: EffectAdapterFixtureCase) -> EffectAdapterFixtureResult:
    request = _build_sanitized_request(case)
    receipt = evaluate_effect_plan(request)
    signatures = tuple(
        {
            "action": f"{effect.action_type}:{effect.sequence}",
            "status": effect.status.value,
            "reason": effect.reason.value,
        }
        for effect in receipt.effects
    )
    return EffectAdapterFixtureResult(
        case_id=case.case_id,
        passed=signatures == case.expected_receipts,
        receipt_id=receipt.receipt_id,
        receipts=signatures,
    )
```

The builder uses opaque identifiers only, constructs all domain contracts via
their `.create()` factories, and applies only a closed mutation enum. It never
imports a service module. Malformed-plan cases use `dataclasses.replace()` only
after creating a valid action/plan, preserving the exact validator-failure test
without adding a production bypass.

- [ ] **Step 5: Run fixture, evaluator, and isolation tests**

```bash
.venv/bin/python -m unittest \
  tests.test_claim_pipeline_effect_adapter \
  tests.test_claim_pipeline_effect_adapter_fixtures \
  tests.test_claim_pipeline_isolation \
  -v
```

Expected: all tests pass, with 18/18 fixture cases matching exact receipts.

- [ ] **Step 6: Commit the fixture lattice**

```bash
git add \
  email_automation/claim_pipeline/effect_adapter_fixtures.py \
  tests/fixtures/claim_pipeline_effect_adapter_cases.json \
  tests/test_claim_pipeline_effect_adapter_fixtures.py
git commit -m "test: add effect adapter safety lattice"
```

## Task 4: Deterministic Clean-Tree Runner and Privacy Report

**Files:**
- Create: `scripts/run_claim_pipeline_effect_adapter_dry_run.py`
- Create: `tests/test_claim_pipeline_effect_adapter_report.py`

- [ ] **Step 1: Write failing runner identity and privacy tests**

Tests must require:

```python
EXPECTED_RUNS = 3
EXPECTED_CASES = 18

self.assertTrue(report["passed"])
self.assertEqual(EXPECTED_RUNS, report["identity"]["runs"])
self.assertEqual(EXPECTED_CASES, report["identity"]["caseCount"])
self.assertFalse(report["identity"]["sourceTreeDirty"])
self.assertEqual(54, report["summary"]["resultCount"])
self.assertEqual(54, report["summary"]["passedResultCount"])
self.assertEqual([], report["summary"]["varianceCaseIds"])
self.assertNotRegex(encoded_report, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
self.assertNotIn("payload", encoded_report)
self.assertNotIn("recipient", encoded_report)
self.assertNotIn("evidenceText", encoded_report)
self.assertNotIn("row-fixture", encoded_report)
```

Also test that a dirty source tree and any fixture expectation mismatch stop the
runner before a passed report is written.

- [ ] **Step 2: Run report tests and verify RED**

```bash
.venv/bin/python -m unittest tests.test_claim_pipeline_effect_adapter_report -v
```

Expected: failure because the runner script is absent.

- [ ] **Step 3: Implement the runner**

The CLI is fixed:

```bash
.venv/bin/python scripts/run_claim_pipeline_effect_adapter_dry_run.py \
  --fixture tests/fixtures/claim_pipeline_effect_adapter_cases.json \
  --runs 3 \
  --output /tmp/sitesift-disabled-effect-adapter-report.json
```

The runner rejects any `--runs` value other than `3`, requires a clean committed
tree, hashes the source tree and fixture, runs every case three times, and emits
only case ID, pass/fail, status/reason signatures, receipt digest, and identity
hashes. It computes one canonical result digest and fails if a case has more
than one receipt digest across repeats.

Use this report skeleton:

```python
report = {
    "identity": {
        "profile": "disabled-effect-adapter-dry-run-v1",
        "codeRevision": revision,
        "sourceTreeDirty": False,
        "sourceTreeHash": source_tree_hash,
        "fixtureHash": fixture_hash,
        "caseCount": len(catalog.cases),
        "runs": args.runs,
    },
    "summary": {
        "resultCount": len(results),
        "passedResultCount": sum(item["passed"] for item in results),
        "varianceCaseIds": variance_case_ids,
        "statusCounts": status_counts,
        "reasonCounts": reason_counts,
    },
    "results": results,
}
report["passed"] = (
    report["summary"]["resultCount"] == len(catalog.cases) * args.runs
    and report["summary"]["passedResultCount"] == len(results)
    and not variance_case_ids
)
report["resultDigest"] = _digest(report)
```

- [ ] **Step 4: Commit the runner before clean-tree execution**

```bash
git add scripts/run_claim_pipeline_effect_adapter_dry_run.py tests/test_claim_pipeline_effect_adapter_report.py
git commit -m "test: add deterministic effect adapter runner"
```

- [ ] **Step 5: Run report tests and the clean-tree runner**

Run the report tests, then the exact CLI from Step 3. Expected: 54/54 results
pass, every case has one repeat digest, and the report privacy scan is clean.

## Task 5: Public API and Structural Isolation

**Files:**
- Modify: `email_automation/claim_pipeline/__init__.py`
- Modify: `tests/test_claim_pipeline_isolation.py`

- [ ] **Step 1: Add failing package-boundary and forbidden-import tests**

Require these public names:

```python
expected_names = {
    "ActionStateSnapshot",
    "ApprovalGrant",
    "DryRunCommitReceipt",
    "DryRunEffectReceipt",
    "DryRunReason",
    "DryRunStatus",
    "EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION",
    "EffectAdapterFixtureCatalog",
    "EffectAdapterFixtureCase",
    "EffectAdapterFixtureResult",
    "EffectAdapterFixtureValidationError",
    "EffectAdapterRequest",
    "evaluate_effect_plan",
    "load_effect_adapter_fixture_catalog",
    "run_effect_adapter_fixture_case",
}
self.assertEqual(set(), {name for name in expected_names if not hasattr(claim_pipeline, name)})
```

Extend the AST scanner test so relative imports of `..processing`,
`..ai_processing`, `..pending_responses`, `..sheets`, `..followup`, and
`..notifications` all resolve outside the allowed package and fail. Add an AST
walk over `effect_adapter.py` and `effect_adapter_fixtures.py` that rejects
`Callable`, `Protocol`, lambda nodes, function-valued dataclass fields, and the
closed forbidden token list from the design.

- [ ] **Step 2: Run isolation tests and verify RED**

```bash
.venv/bin/python -m unittest tests.test_claim_pipeline_isolation -v
```

Expected: missing package exports.

- [ ] **Step 3: Export the pure API only**

Import and add the exact names from Step 1 to `__all__`. Do not export private
builders or report helpers.

- [ ] **Step 4: Run all claim-pipeline tests**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_claim_pipeline*.py' -v
```

Expected: all claim-pipeline tests pass with no forbidden imports.

- [ ] **Step 5: Commit the public boundary**

```bash
git add email_automation/claim_pipeline/__init__.py tests/test_claim_pipeline_isolation.py
git commit -m "test: lock effect adapter isolation"
```

## Task 6: Full Verification and Evidence

**Files:**
- Create: `docs/release-safety/disabled-effect-adapter-evidence-2026-07-22.md`

- [ ] **Step 1: Run compilation and focused verification**

```bash
.venv/bin/python -m compileall -q email_automation/claim_pipeline scripts/run_claim_pipeline_effect_adapter_dry_run.py
.venv/bin/python -m unittest discover -s tests -p 'test_claim_pipeline*.py' -v
git diff --check
```

Expected: all commands exit zero.

- [ ] **Step 2: Run the full backend suite**

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all backend tests pass. Record the exact test count and elapsed time;
do not copy a previous count.

- [ ] **Step 3: Re-run the clean-tree report from the final code revision**

Commit any test-only corrections first, verify `git status --short` is empty,
then run the exact Task 4 CLI. Assert:

```python
assert report["passed"] is True
assert report["summary"]["resultCount"] == 54
assert report["summary"]["passedResultCount"] == 54
assert report["summary"]["varianceCaseIds"] == []
assert report["identity"]["sourceTreeDirty"] is False
```

- [ ] **Step 4: Write the evidence document**

Record:

- final code revision and source-tree hash;
- fixture schema/hash and exact 18-case inventory;
- report SHA-256 and result digest;
- 54/54 exact-oracle results and zero variance;
- status and reason counts;
- focused/full test counts and elapsed time;
- import/callable isolation result;
- privacy result;
- explicit statement that no provider, Graph, Firebase, Sheets, mailbox, queue,
  notification, follow-up, draft, send, deployment, Jill/live data, or production
  configuration was touched;
- decision: this unlocks design of disabled staging persistence only.

- [ ] **Step 5: Verify and commit evidence**

```bash
rg -n 'TBD|TODO|FIXME' docs/release-safety/disabled-effect-adapter-evidence-2026-07-22.md
git diff --check
git add docs/release-safety/disabled-effect-adapter-evidence-2026-07-22.md
git commit -m "docs: record disabled effect adapter evidence"
git status --short
```

Expected: the placeholder scan has no matches, diff check passes, the commit
succeeds, and the final worktree is clean.

## Stop Conditions

Stop immediately and report the exact evidence if any implementation:

1. Imports or accepts a Graph, Firebase, Firestore, Sheets, mailbox, queue,
   notification, follow-up, processing, pending-response, outbox, or send surface.
2. Uses `applied` as a dry-run status or claims an eligible action was executed.
3. Emits payloads, recipients, evidence text, addresses, message bodies,
   exception stacks, or customer identifiers in fixtures/reports.
4. Allows a stale snapshot, stale contract, wrong prior state, duplicate semantic
   effect, blocked dependency, terminal outbound action, or wrong approval scope
   to become `would_apply`.
5. Weakens the existing claim, policy, or plan validator to make a fixture pass.
6. Modifies the legacy worker, production configuration, deployment, or live data.

## Completion Boundary

Completion means the pure adapter gate is proven in isolation and documented.
It does not mean effects, persistence, staging integration, browser campaigns,
or production are approved. The next Active Experiment after a clean pass is a
separate disabled staging-persistence design with read-only Admin evidence.
