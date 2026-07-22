import json
import unittest
from dataclasses import replace

from email_automation.claim_pipeline.contracts import (
    ActionPlan,
    ActionType,
    Actor,
    ActorRole,
    ApprovalClass,
    CampaignContract,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    CommitReceipt,
    CompletenessState,
    ConversationState,
    ContractAuthority,
    DecisionSnapshot,
    Direction,
    EntityRef,
    EntityType,
    EvidenceEnvelope,
    EvidenceFreshness,
    EvidenceSource,
    EffectReceipt,
    EffectStatus,
    FitState,
    MarketState,
    PlannedAction,
)


def _actor() -> Actor:
    return Actor(
        name="Alex Broker",
        email="alex@example.com",
        role=ActorRole.BROKER,
    )


def _evidence(*, tenant_id: str = "uid-1") -> EvidenceEnvelope:
    return EvidenceEnvelope.create(
        tenant_id=tenant_id,
        message_id="graph-message-1",
        source_kind=EvidenceSource.FRESH_BODY,
        location="body:0-26",
        content="Suite B is available now.",
        direction=Direction.INBOUND,
        actor=_actor(),
        observed_at="2026-07-22T12:00:00Z",
        freshness=EvidenceFreshness.FRESH,
    )


def _entity(*, tenant_id: str = "uid-1") -> EntityRef:
    return EntityRef.create(
        tenant_id=tenant_id,
        campaign_id="campaign-1",
        entity_type=EntityType.SUITE,
        label="Suite B",
        canonical_address="123 Industrial Ave",
        suite="B",
        relationship="target",
    )


def _claim(evidence: EvidenceEnvelope, entity: EntityRef) -> Claim:
    return Claim.create(
        tenant_id=evidence.tenant_id,
        evidence_id=evidence.evidence_id,
        subject_entity_id=entity.entity_id,
        predicate=ClaimPredicate.AVAILABILITY,
        value="available",
        evidence_text="Suite B is available now.",
        actor_role=ActorRole.BROKER,
        polarity=ClaimPolarity.POSITIVE,
        modality=ClaimModality.ASSERTED,
        confidence=0.99,
    )


def _contract() -> CampaignContract:
    return CampaignContract.create(
        tenant_id="uid-1",
        client_id="client-1",
        campaign_id="campaign-1",
        version=3,
        transaction_types=("lease",),
        required_fields=("total_sf", "rent"),
        hard_requirements={"occupancy_by": "2026-09-01"},
        soft_preferences={"drive_ins": 1},
        source_authority=ContractAuthority.USER,
    )


class ClaimPipelineContractTests(unittest.TestCase):
    def test_evidence_identity_is_stable_and_tenant_scoped(self):
        first = _evidence()
        second = _evidence()
        other_tenant = _evidence(tenant_id="uid-2")

        self.assertEqual(first.evidence_id, second.evidence_id)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertNotEqual(first.evidence_id, other_tenant.evidence_id)

    def test_entity_and_claim_identity_are_stable(self):
        evidence = _evidence()
        entity = _entity()

        self.assertEqual(_entity().entity_id, entity.entity_id)
        self.assertEqual(
            _claim(evidence, entity).claim_id,
            _claim(evidence, entity).claim_id,
        )

    def test_claim_identity_includes_the_exact_evidence_span(self):
        evidence = EvidenceEnvelope.create(
            tenant_id="uid-1",
            message_id="graph-message-2",
            source_kind=EvidenceSource.FRESH_BODY,
            location="body:0-55",
            content="Suite B is available now. The space remains available.",
            direction=Direction.INBOUND,
            actor=_actor(),
            observed_at="2026-07-22T12:00:00Z",
            freshness=EvidenceFreshness.FRESH,
        )
        entity = _entity()
        common = {
            "tenant_id": "uid-1",
            "evidence_id": evidence.evidence_id,
            "subject_entity_id": entity.entity_id,
            "predicate": ClaimPredicate.AVAILABILITY,
            "value": "available",
            "actor_role": ActorRole.BROKER,
            "polarity": ClaimPolarity.POSITIVE,
            "modality": ClaimModality.ASSERTED,
            "confidence": 0.99,
        }

        first = Claim.create(evidence_text="Suite B is available now.", **common)
        second = Claim.create(evidence_text="The space remains available.", **common)

        self.assertNotEqual(first.claim_id, second.claim_id)

    def test_contract_and_decision_serialize_to_plain_json_values(self):
        evidence = _evidence()
        entity = _entity()
        contract = _contract()
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.AVAILABLE,
            fit_state=FitState.VIABLE,
            completeness_state=CompletenessState.INCOMPLETE,
            conversation_state=ConversationState.ACTIVE,
            reason_codes=("broker_confirmed_available",),
            evidence_ids=(evidence.evidence_id,),
            missing_fields=("rent",),
        )

        encoded = json.dumps(
            {"contract": contract.to_dict(), "decision": decision.to_dict()},
            sort_keys=True,
        )

        self.assertIn('"market_state": "available"', encoded)
        self.assertIn(
            '"hard_requirements": {"occupancy_by": "2026-09-01"}',
            encoded,
        )

    def test_planned_action_payload_is_immutable_and_plan_identity_is_stable(self):
        contract = _contract()
        entity = _entity()
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.AVAILABLE,
            fit_state=FitState.VIABLE,
            completeness_state=CompletenessState.INCOMPLETE,
            conversation_state=ConversationState.ACTIVE,
        )
        action = PlannedAction.create(
            tenant_id="uid-1",
            client_id="client-1",
            campaign_id=contract.campaign_id,
            thread_id="thread-1",
            sheet_id="sheet-1",
            row_anchor="123 Industrial Ave|Suite B",
            decision_id=decision.decision_id,
            contract_id=contract.contract_id,
            action_type=ActionType.FACT_UPDATE,
            approval_class=ApprovalClass.AUTOMATIC,
            target_entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash=decision.snapshot_hash,
            source_claim_ids=("claim-1",),
            operation_key="message-1:availability",
            expected_prior_state={"availability": ""},
            dependencies=(),
            sequence=1,
            recipient="",
            payload={"field": "availability", "value": "available"},
            reason="broker_confirmed_available",
        )
        first = ActionPlan.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            decision_id=decision.decision_id,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            snapshot_hash=decision.snapshot_hash,
            actions=(action,),
        )
        second = ActionPlan.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            decision_id=decision.decision_id,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            snapshot_hash=decision.snapshot_hash,
            actions=(action,),
        )

        self.assertEqual(first.plan_id, second.plan_id)
        self.assertTrue(action.idempotency_key)
        with self.assertRaises(TypeError):
            action.payload["value"] = "unavailable"

    def test_action_identity_includes_approval_class(self):
        contract = _contract()
        entity = _entity()
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.AVAILABLE,
            fit_state=FitState.REVIEW,
            completeness_state=CompletenessState.BLOCKED,
            conversation_state=ConversationState.REVIEW,
        )
        common = {
            "tenant_id": "uid-1",
            "client_id": "client-1",
            "campaign_id": contract.campaign_id,
            "thread_id": "thread-1",
            "sheet_id": "sheet-1",
            "row_anchor": "123 Industrial Ave|Suite B",
            "decision_id": decision.decision_id,
            "contract_id": contract.contract_id,
            "action_type": ActionType.CALL_REQUEST,
            "target_entity_id": entity.entity_id,
            "contract_version": contract.version,
            "snapshot_hash": decision.snapshot_hash,
            "source_claim_ids": ("claim-1",),
            "operation_key": "message-1:call",
            "expected_prior_state": {"conversationState": "review"},
            "dependencies": (),
            "sequence": 1,
            "recipient": "",
            "payload": {"phone": "known"},
            "reason": "broker_requested_call",
        }

        automatic = PlannedAction.create(
            approval_class=ApprovalClass.AUTOMATIC,
            **common,
        )
        reviewed = PlannedAction.create(
            approval_class=ApprovalClass.HUMAN_REQUIRED,
            **common,
        )

        self.assertNotEqual(automatic.action_id, reviewed.action_id)
        self.assertEqual(automatic.idempotency_key, reviewed.idempotency_key)

    def test_effect_identity_survives_rewording_and_recomputation(self):
        contract = _contract()
        entity = _entity()
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.AVAILABLE,
            fit_state=FitState.VIABLE,
            completeness_state=CompletenessState.INCOMPLETE,
            conversation_state=ConversationState.ACTIVE,
        )
        common = {
            "tenant_id": "uid-1",
            "client_id": contract.client_id,
            "campaign_id": contract.campaign_id,
            "thread_id": "thread-1",
            "sheet_id": "sheet-1",
            "row_anchor": "123 Industrial Ave|Suite B",
            "decision_id": decision.decision_id,
            "contract_id": contract.contract_id,
            "action_type": ActionType.FACT_UPDATE,
            "approval_class": ApprovalClass.AUTOMATIC,
            "target_entity_id": entity.entity_id,
            "contract_version": contract.version,
            "source_claim_ids": ("claim-1",),
            "operation_key": "message-1:availability",
            "expected_prior_state": {"availability": ""},
            "dependencies": (),
            "sequence": 1,
            "recipient": "",
        }
        first = PlannedAction.create(
            snapshot_hash="snapshot-1",
            payload={"field": "availability", "value": "available"},
            reason="broker_confirmed_available",
            **common,
        )
        recomputed = PlannedAction.create(
            snapshot_hash="snapshot-2",
            payload={"field": "availability", "value": "available", "confidence": 0.99},
            reason="availability_confirmed_by_broker",
            **common,
        )

        self.assertNotEqual(first.action_id, recomputed.action_id)
        self.assertEqual(first.idempotency_key, recomputed.idempotency_key)

    def test_effect_identity_changes_for_a_different_sheet_row(self):
        contract = _contract()
        entity = _entity()
        common = {
            "tenant_id": "uid-1",
            "client_id": contract.client_id,
            "campaign_id": contract.campaign_id,
            "thread_id": "thread-1",
            "sheet_id": "sheet-1",
            "decision_id": "decision-1",
            "contract_id": contract.contract_id,
            "action_type": ActionType.FACT_UPDATE,
            "approval_class": ApprovalClass.AUTOMATIC,
            "target_entity_id": entity.entity_id,
            "contract_version": contract.version,
            "snapshot_hash": "snapshot-1",
            "source_claim_ids": ("claim-1",),
            "operation_key": "message-1:availability",
            "expected_prior_state": {"availability": ""},
            "dependencies": (),
            "sequence": 1,
            "recipient": "",
            "payload": {"field": "availability", "value": "available"},
            "reason": "broker_confirmed_available",
        }
        first = PlannedAction.create(
            row_anchor="123 Industrial Ave|Suite B",
            **common,
        )
        second = PlannedAction.create(
            row_anchor="900 Replacement Rd|Suite 100",
            **common,
        )

        self.assertNotEqual(first.idempotency_key, second.idempotency_key)

    def test_derived_identities_reject_tampering_after_construction(self):
        evidence = _evidence()
        entity = _entity()
        claim = _claim(evidence, entity)
        contract = _contract()
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.AVAILABLE,
            fit_state=FitState.VIABLE,
            completeness_state=CompletenessState.INCOMPLETE,
            conversation_state=ConversationState.ACTIVE,
        )
        action = PlannedAction.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            thread_id="thread-1",
            sheet_id="sheet-1",
            row_anchor="123 Industrial Ave|Suite B",
            decision_id=decision.decision_id,
            contract_id=contract.contract_id,
            action_type=ActionType.FACT_UPDATE,
            approval_class=ApprovalClass.AUTOMATIC,
            target_entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash=decision.snapshot_hash,
            source_claim_ids=(claim.claim_id,),
            operation_key="message-1:availability",
            expected_prior_state={"availability": ""},
            dependencies=(),
            sequence=1,
            recipient="",
            payload={"field": "availability", "value": "available"},
            reason="broker_confirmed_available",
        )
        plan = ActionPlan.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            decision_id=decision.decision_id,
            contract_id=contract.contract_id,
            contract_version=contract.version,
            snapshot_hash=decision.snapshot_hash,
            actions=(action,),
        )

        with self.assertRaisesRegex(ValueError, "entity identity"):
            replace(entity, label="Different Suite")
        with self.assertRaisesRegex(ValueError, "claim identity"):
            replace(claim, value="unavailable")
        with self.assertRaisesRegex(ValueError, "contract identity"):
            replace(contract, client_id="client-2")
        with self.assertRaisesRegex(ValueError, "decision identity"):
            replace(decision, snapshot_hash="snapshot-forged")
        with self.assertRaisesRegex(ValueError, "effect identity"):
            replace(action, idempotency_key="effect-forged")
        with self.assertRaisesRegex(ValueError, "plan identity"):
            replace(plan, plan_id="plan-forged")

    def test_nested_mutable_sequence_values_are_rejected(self):
        contract = _contract()
        entity = _entity()
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.UNKNOWN,
            fit_state=FitState.REVIEW,
            completeness_state=CompletenessState.BLOCKED,
            conversation_state=ConversationState.REVIEW,
        )

        with self.assertRaisesRegex(TypeError, "only strings"):
            replace(decision, reason_codes=[["nested"]])

    def test_broker_cannot_be_a_campaign_contract_authority(self):
        with self.assertRaisesRegex(ValueError, "source authority"):
            CampaignContract.create(
                tenant_id="uid-1",
                client_id="client-1",
                campaign_id="campaign-1",
                version=1,
                source_authority="broker",
            )

    def test_claim_confidence_outside_probability_range_is_rejected(self):
        evidence = _evidence()
        entity = _entity()

        with self.assertRaisesRegex(ValueError, "confidence"):
            Claim.create(
                tenant_id="uid-1",
                evidence_id=evidence.evidence_id,
                subject_entity_id=entity.entity_id,
                predicate=ClaimPredicate.AVAILABILITY,
                value="available",
                evidence_text="Suite B is available now.",
                actor_role=ActorRole.BROKER,
                polarity=ClaimPolarity.POSITIVE,
                modality=ClaimModality.ASSERTED,
                confidence=1.5,
            )

    def test_invalid_runtime_enum_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "actor role"):
            Actor(name="Alex", email="alex@example.com", role="brokerish")

    def test_contract_payloads_reject_non_string_keys_and_non_finite_numbers(self):
        contract = _contract()
        entity = _entity()
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.AVAILABLE,
            fit_state=FitState.VIABLE,
            completeness_state=CompletenessState.INCOMPLETE,
            conversation_state=ConversationState.ACTIVE,
        )
        common = {
            "tenant_id": "uid-1",
            "client_id": "client-1",
            "campaign_id": contract.campaign_id,
            "thread_id": "thread-1",
            "sheet_id": "sheet-1",
            "row_anchor": "123 Industrial Ave|Suite B",
            "decision_id": decision.decision_id,
            "contract_id": contract.contract_id,
            "action_type": ActionType.FACT_UPDATE,
            "approval_class": ApprovalClass.AUTOMATIC,
            "target_entity_id": entity.entity_id,
            "contract_version": contract.version,
            "snapshot_hash": decision.snapshot_hash,
            "source_claim_ids": ("claim-1",),
            "operation_key": "message-1:payload-validation",
            "expected_prior_state": {"rowAnchor": "123 Industrial Ave|Suite B"},
            "dependencies": (),
            "sequence": 1,
            "recipient": "",
            "reason": "test_payload_validation",
        }

        with self.assertRaisesRegex(TypeError, "keys must be strings"):
            PlannedAction.create(payload={1: "unsafe"}, **common)
        with self.assertRaisesRegex(TypeError, "finite"):
            PlannedAction.create(payload={"confidence": float("nan")}, **common)

    def test_effect_receipt_requires_positive_attempt_and_action_identity(self):
        with self.assertRaisesRegex(ValueError, "action_id"):
            EffectReceipt(action_id="", status=EffectStatus.PENDING, attempt=1)
        with self.assertRaisesRegex(ValueError, "attempt"):
            EffectReceipt(action_id="action-1", status=EffectStatus.PENDING, attempt=0)

    def test_direct_construction_normalizes_sequence_fields_to_tuples(self):
        entity_evidence = ["evidence-1"]
        entity = EntityRef.create(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            entity_type=EntityType.SUITE,
            label="Suite B",
            evidence_ids=entity_evidence,
        )
        entity = replace(entity, evidence_ids=entity_evidence)
        decision_reasons = ["reason-1"]
        decision = DecisionSnapshot.create(
            tenant_id="uid-1",
            client_id="client-1",
            campaign_id="campaign-1",
            contract_id="contract-direct",
            entity_id=entity.entity_id,
            contract_version=1,
            snapshot_hash="snapshot-1",
            market_state=MarketState.UNKNOWN,
            fit_state=FitState.REVIEW,
            completeness_state=CompletenessState.BLOCKED,
            conversation_state=ConversationState.REVIEW,
            reason_codes=decision_reasons,
        )
        decision = replace(decision, reason_codes=decision_reasons)

        entity_evidence.append("evidence-2")
        decision_reasons.append("reason-2")

        self.assertEqual(("evidence-1",), entity.evidence_ids)
        self.assertEqual(("reason-1",), decision.reason_codes)

    def test_commit_receipt_is_immutable_and_json_safe(self):
        effect = EffectReceipt(
            action_id="action-1",
            status=EffectStatus.APPLIED,
            attempt=1,
            external_id="sheet-write-1",
        )
        receipt = CommitReceipt(
            tenant_id="uid-1",
            plan_id="plan-1",
            effects=(effect,),
            completed_at="2026-07-22T14:00:00Z",
        )

        encoded = json.dumps(receipt.to_dict(), sort_keys=True)

        self.assertIn('"status": "applied"', encoded)
        with self.assertRaises(AttributeError):
            receipt.plan_id = "plan-2"


if __name__ == "__main__":
    unittest.main()
