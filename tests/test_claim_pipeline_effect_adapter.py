import json
import unittest
from dataclasses import replace

from email_automation.claim_pipeline.contracts import (
    ActionPlan,
    ActionType,
    ActorRole,
    ApprovalClass,
    CampaignContract,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    CompletenessState,
    ConversationState,
    ContractAuthority,
    DecisionSnapshot,
    EntityRef,
    EntityType,
    ExecutionScope,
    FitState,
    MarketState,
    PlannedAction,
)
from email_automation.claim_pipeline.effect_adapter import (
    ActionStateSnapshot,
    ApprovalGrant,
    DryRunCommitReceipt,
    DryRunEffectReceipt,
    DryRunReason,
    DryRunStatus,
    EffectAdapterRequest,
)


def _minimal_request_fields():
    tenant_id = "tenant-fixture"
    client_id = "client-fixture"
    campaign_id = "campaign-fixture"
    snapshot_hash = "snapshot-fixture"
    entity = EntityRef.create(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        entity_type=EntityType.PROPERTY,
        label="entity-fixture",
    )
    contract = CampaignContract.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        version=1,
        transaction_types=("transaction-fixture",),
        required_fields=("field-fixture",),
        source_authority=ContractAuthority.SETUP,
    )
    claim = Claim.create(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        evidence_id="evidence-fixture",
        subject_entity_id=entity.entity_id,
        predicate=ClaimPredicate.AVAILABILITY,
        value="available",
        evidence_text="evidence-fixture",
        actor_role=ActorRole.BROKER,
        polarity=ClaimPolarity.POSITIVE,
        modality=ClaimModality.ASSERTED,
        confidence=0.99,
    )
    decision = DecisionSnapshot.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        contract_id=contract.contract_id,
        entity_id=entity.entity_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        market_state=MarketState.AVAILABLE,
        fit_state=FitState.VIABLE,
        completeness_state=CompletenessState.INCOMPLETE,
        conversation_state=ConversationState.ACTIVE,
        evidence_ids=("evidence-fixture",),
    )
    scope = ExecutionScope(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        thread_id="thread-fixture",
        sheet_id="sheet-fixture",
        row_anchor="row-fixture",
    )
    action = PlannedAction.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        thread_id=scope.thread_id,
        sheet_id=scope.sheet_id,
        row_anchor=scope.row_anchor,
        decision_id=decision.decision_id,
        contract_id=contract.contract_id,
        action_type=ActionType.FACT_UPDATE,
        approval_class=ApprovalClass.AUTOMATIC,
        target_entity_id=entity.entity_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        source_claim_ids=(claim.claim_id,),
        operation_key="operation-fixture",
        expected_prior_state={"conversationState": "active"},
        dependencies=(),
        sequence=1,
        recipient="",
        payload={"field": "availability", "value": "available"},
        reason="reason-fixture",
    )
    plan = ActionPlan.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        decision_id=decision.decision_id,
        contract_id=contract.contract_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        actions=(action,),
    )
    return {
        "plan": plan,
        "decision": decision,
        "scope": scope,
        "entities": (entity,),
        "claims": (claim,),
        "authorized_recipients": (),
        "current_snapshot_hash": snapshot_hash,
        "current_contract_id": contract.contract_id,
        "current_contract_version": contract.version,
    }


def _effect_receipt(**overrides):
    values = {
        "plan_id": "plan-fixture",
        "action_id": "action-fixture",
        "idempotency_key": "effect-fixture",
        "action_type": "fact_update",
        "sequence": 1,
        "status": DryRunStatus.WOULD_APPLY,
        "reason": DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
        "dependency_receipt_ids": (),
    }
    values.update(overrides)
    return DryRunEffectReceipt.create(**values)


def _commit_receipt(*, effects=(), **overrides):
    values = {
        "tenant_id": "tenant-fixture",
        "plan_id": "plan-fixture",
        "decision_id": "decision-fixture",
        "contract_id": "contract-fixture",
        "contract_version": 1,
        "snapshot_hash": "snapshot-fixture",
        "effects": effects,
    }
    values.update(overrides)
    return DryRunCommitReceipt.create(**values)


class EffectAdapterContractTests(unittest.TestCase):
    def test_dry_run_status_cannot_report_applied(self):
        self.assertEqual(
            {"would_apply", "blocked", "skipped"},
            {status.value for status in DryRunStatus},
        )
        self.assertNotIn("applied", {status.value for status in DryRunStatus})

    def test_dry_run_reason_vocabulary_is_closed(self):
        self.assertEqual(
            {
                "eligible_automatic_action",
                "eligible_human_approved_action",
                "approval_required",
                "approval_scope_mismatch",
                "unsupported_action_type",
                "stale_snapshot",
                "stale_contract",
                "prior_state_mismatch",
                "idempotency_key_already_committed",
                "dependency_blocked",
                "terminal_outbound_suppressed",
                "plan_contract_violation",
            },
            {reason.value for reason in DryRunReason},
        )

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

    def test_action_state_snapshot_is_deeply_immutable(self):
        source = {
            "conversationState": "active",
            "nested": {"values": ["value-fixture"]},
        }
        state = ActionStateSnapshot.create(
            action_id="action-fixture",
            values=source,
        )

        source["conversationState"] = "different-state"
        source["nested"]["values"].append("different-value")

        self.assertEqual(
            {
                "conversationState": "active",
                "nested": {"values": ["value-fixture"]},
            },
            state.to_dict()["values"],
        )
        with self.assertRaises(TypeError):
            state.values["conversationState"] = "different-state"
        with self.assertRaises(TypeError):
            state.values["nested"]["different-key"] = "different-value"

    def test_state_and_receipt_identities_reject_replace_tampering(self):
        state = ActionStateSnapshot.create(
            action_id="action-fixture",
            values={"conversationState": "active"},
        )
        effect = _effect_receipt()
        commit = _commit_receipt(effects=(effect,))

        with self.assertRaisesRegex(ValueError, "action state identity"):
            replace(state, action_id="different-action")
        with self.assertRaisesRegex(ValueError, "effect receipt identity"):
            replace(effect, action_type="different-action-type")
        with self.assertRaisesRegex(ValueError, "commit receipt identity"):
            replace(commit, decision_id="different-decision")

    def test_effect_receipt_enforces_closed_status_reason_mapping(self):
        valid_reasons = {
            DryRunStatus.WOULD_APPLY: {
                DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
                DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION,
            },
            DryRunStatus.SKIPPED: {
                DryRunReason.APPROVAL_REQUIRED,
                DryRunReason.IDEMPOTENCY_KEY_ALREADY_COMMITTED,
            },
            DryRunStatus.BLOCKED: {
                DryRunReason.APPROVAL_SCOPE_MISMATCH,
                DryRunReason.UNSUPPORTED_ACTION_TYPE,
                DryRunReason.STALE_SNAPSHOT,
                DryRunReason.STALE_CONTRACT,
                DryRunReason.PRIOR_STATE_MISMATCH,
                DryRunReason.DEPENDENCY_BLOCKED,
                DryRunReason.TERMINAL_OUTBOUND_SUPPRESSED,
                DryRunReason.PLAN_CONTRACT_VIOLATION,
            },
        }

        for status in DryRunStatus:
            for reason in DryRunReason:
                with self.subTest(status=status.value, reason=reason.value):
                    if reason in valid_reasons[status]:
                        receipt = _effect_receipt(status=status, reason=reason)
                        self.assertEqual((status, reason), (receipt.status, receipt.reason))
                    else:
                        with self.assertRaisesRegex(ValueError, "status/reason"):
                            _effect_receipt(status=status, reason=reason)

    def test_request_rejects_duplicate_action_state_and_history(self):
        common = _minimal_request_fields()
        action_id = common["plan"].actions[0].action_id
        state = ActionStateSnapshot.create(
            action_id=action_id,
            values={"conversationState": "active"},
        )
        grant = ApprovalGrant.create(
            tenant_id="tenant-fixture",
            plan_id=common["plan"].plan_id,
            action_id=action_id,
            snapshot_hash="snapshot-fixture",
            approved_by="operator-fixture",
        )
        with self.assertRaisesRegex(ValueError, "duplicate action state"):
            EffectAdapterRequest.create(
                current_states=(state, state),
                committed_idempotency_keys=(),
                approval_grants=(),
                **common,
            )
        with self.assertRaisesRegex(ValueError, "duplicate approval grant"):
            EffectAdapterRequest.create(
                current_states=(state,),
                committed_idempotency_keys=(),
                approval_grants=(grant, grant),
                **common,
            )
        with self.assertRaisesRegex(ValueError, "duplicate committed idempotency"):
            EffectAdapterRequest.create(
                current_states=(state,),
                committed_idempotency_keys=("effect-fixture", "effect-fixture"),
                approval_grants=(),
                **common,
            )

    def test_request_requires_exactly_one_state_per_plan_action(self):
        common = _minimal_request_fields()
        with self.assertRaisesRegex(ValueError, "one action state"):
            EffectAdapterRequest.create(
                current_states=(),
                committed_idempotency_keys=(),
                approval_grants=(),
                **common,
            )

    def test_commit_rejects_foreign_plan_and_duplicate_effect_dimensions(self):
        effect = _effect_receipt()
        foreign_plan = _effect_receipt(plan_id="different-plan")
        duplicate_action = _effect_receipt(
            idempotency_key="different-effect",
            action_type="note_append",
            sequence=2,
        )
        duplicate_idempotency = _effect_receipt(
            action_id="different-action",
            action_type="note_append",
            sequence=2,
        )
        duplicate_sequence = _effect_receipt(
            action_id="different-action",
            idempotency_key="different-effect",
            action_type="note_append",
        )

        cases = (
            ((foreign_plan,), "effect plan_id"),
            ((effect, effect), "duplicate effect receipt_id"),
            ((effect, duplicate_action), "duplicate effect action_id"),
            ((effect, duplicate_idempotency), "duplicate effect idempotency_key"),
            ((effect, duplicate_sequence), "duplicate effect sequence"),
        )
        for effects, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    _commit_receipt(effects=effects)

    def test_commit_allows_zero_effects(self):
        commit = _commit_receipt()

        self.assertEqual((), commit.effects)
        self.assertEqual(
            {"would_apply": 0, "blocked": 0, "skipped": 0},
            dict(commit.status_counts),
        )

    def test_request_contract_version_requires_positive_int(self):
        common = _minimal_request_fields()
        state = ActionStateSnapshot.create(
            action_id=common["plan"].actions[0].action_id,
            values={"conversationState": "active"},
        )
        for invalid_version in (True, 1.0):
            with self.subTest(value=invalid_version):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    EffectAdapterRequest.create(
                        current_contract_version=invalid_version,
                        current_states=(state,),
                        committed_idempotency_keys=(),
                        approval_grants=(),
                        **{
                            key: value
                            for key, value in common.items()
                            if key != "current_contract_version"
                        },
                    )

    def test_effect_sequence_requires_positive_int(self):
        for invalid_sequence in (True, 1.0):
            with self.subTest(value=invalid_sequence):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    _effect_receipt(sequence=invalid_sequence)

    def test_commit_contract_version_requires_positive_int(self):
        for invalid_version in (True, 1.0):
            with self.subTest(value=invalid_version):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    _commit_receipt(contract_version=invalid_version)

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
        repeated_effect = DryRunEffectReceipt.create(
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
        repeated_commit = DryRunCommitReceipt.create(
            tenant_id="tenant-fixture",
            plan_id="plan-fixture",
            decision_id="decision-fixture",
            contract_id="contract-fixture",
            contract_version=1,
            snapshot_hash="snapshot-fixture",
            effects=(repeated_effect,),
        )

        encoded = json.dumps(commit.to_dict(), sort_keys=True)
        serialized = commit.to_dict()

        self.assertEqual(effect.receipt_id, repeated_effect.receipt_id)
        self.assertEqual(commit.receipt_id, repeated_commit.receipt_id)
        self.assertEqual(
            {"would_apply": 1, "blocked": 0, "skipped": 0},
            dict(commit.status_counts),
        )
        self.assertEqual(
            {
                "receipt_id",
                "plan_id",
                "action_id",
                "idempotency_key",
                "action_type",
                "sequence",
                "status",
                "reason",
                "dependency_receipt_ids",
            },
            set(effect.to_dict()),
        )
        self.assertEqual(
            {
                "receipt_id",
                "tenant_id",
                "plan_id",
                "decision_id",
                "contract_id",
                "contract_version",
                "snapshot_hash",
                "effects",
                "status_counts",
            },
            set(serialized),
        )
        self.assertEqual(set(effect.to_dict()), set(serialized["effects"][0]))
        for sensitive_field in (
            "payload",
            "recipient",
            "external_id",
            "timestamp",
            "completed_at",
        ):
            self.assertNotIn(sensitive_field, encoded)


if __name__ == "__main__":
    unittest.main()
