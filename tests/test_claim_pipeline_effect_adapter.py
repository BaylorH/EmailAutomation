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

        self.assertEqual(effect.receipt_id, repeated_effect.receipt_id)
        self.assertEqual(commit.receipt_id, repeated_commit.receipt_id)
        self.assertEqual(
            {"would_apply": 1, "blocked": 0, "skipped": 0},
            dict(commit.status_counts),
        )
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
