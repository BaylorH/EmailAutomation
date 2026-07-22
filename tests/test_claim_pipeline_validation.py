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
    CompletenessState,
    ConversationState,
    DecisionSnapshot,
    Direction,
    EntityRef,
    EntityType,
    EvidenceEnvelope,
    EvidenceFreshness,
    EvidenceSource,
    ExecutionScope,
    FitState,
    MarketState,
    PlannedAction,
)
from email_automation.claim_pipeline.validation import (
    ContractViolation,
    validate_action_plan,
    validate_claim_bundle,
    validate_decision,
)


def _evidence(tenant_id="uid-1"):
    return EvidenceEnvelope.create(
        tenant_id=tenant_id,
        message_id="message-1",
        source_kind=EvidenceSource.FRESH_BODY,
        location="body:0-26",
        content="Suite B is available now.",
        direction=Direction.INBOUND,
        actor=Actor("Alex", "alex@example.com", ActorRole.BROKER),
        observed_at="2026-07-22T12:00:00Z",
        freshness=EvidenceFreshness.FRESH,
    )


def _entity(tenant_id="uid-1"):
    return EntityRef.create(
        tenant_id=tenant_id,
        campaign_id="campaign-1",
        entity_type=EntityType.SUITE,
        label="Suite B",
        canonical_address="123 Industrial Ave",
        suite="B",
    )


def _claim(evidence, entity, *, evidence_text="Suite B is available now."):
    return Claim.create(
        tenant_id=evidence.tenant_id,
        evidence_id=evidence.evidence_id,
        subject_entity_id=entity.entity_id,
        predicate=ClaimPredicate.AVAILABILITY,
        value="available",
        evidence_text=evidence_text,
        actor_role=ActorRole.BROKER,
        polarity=ClaimPolarity.POSITIVE,
        modality=ClaimModality.ASSERTED,
        confidence=0.98,
    )


def _source_claim(entity, predicate=ClaimPredicate.AVAILABILITY):
    if predicate is ClaimPredicate.AVAILABILITY:
        return _claim(_evidence(entity.tenant_id), entity)
    evidence = _evidence(entity.tenant_id)
    return Claim.create(
        tenant_id=entity.tenant_id,
        evidence_id=evidence.evidence_id,
        subject_entity_id=entity.entity_id,
        predicate=predicate,
        value=True,
        evidence_text=evidence.content,
        actor_role=ActorRole.BROKER,
        polarity=ClaimPolarity.POSITIVE,
        modality=ClaimModality.ASSERTED,
        confidence=0.98,
    )


def _contract(tenant_id="uid-1", version=2):
    return CampaignContract.create(
        tenant_id=tenant_id,
        client_id="client-1",
        campaign_id="campaign-1",
        version=version,
        required_fields=("rent",),
        hard_requirements={"occupancy_by": "2026-09-01"},
    )


def _decision(entity, contract, *, snapshot_hash="snapshot-1"):
    return DecisionSnapshot.create(
        tenant_id=entity.tenant_id,
        client_id=contract.client_id,
        campaign_id=contract.campaign_id,
        contract_id=contract.contract_id,
        entity_id=entity.entity_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        market_state=MarketState.AVAILABLE,
        fit_state=FitState.VIABLE,
        completeness_state=CompletenessState.INCOMPLETE,
        conversation_state=ConversationState.ACTIVE,
    )


def _action(
    decision,
    action_type,
    approval_class,
    *,
    sequence=1,
    target_entity_id=None,
    recipient="",
    dependencies=(),
    thread_id="thread-1",
    sheet_id="sheet-1",
    row_anchor="123 Industrial Ave|Suite B",
    source_claim_ids=None,
    payload=None,
    operation_key=None,
):
    support_predicate = {
        ActionType.RECIPIENT_CHANGE: ClaimPredicate.REFERRAL,
        ActionType.ALTERNATE_PROPERTY_PROPOSAL: ClaimPredicate.IDENTITY,
        ActionType.FOLLOWUP_FREEZE: ClaimPredicate.OPT_OUT,
        ActionType.TOUR_REQUEST: ClaimPredicate.TOUR_REQUEST,
        ActionType.CALL_REQUEST: ClaimPredicate.CALL_REQUEST,
    }.get(action_type, ClaimPredicate.AVAILABILITY)
    source_claim_ids = source_claim_ids or (
        _source_claim(_entity(decision.tenant_id), support_predicate).claim_id,
    )
    default_payload = {
        ActionType.FACT_UPDATE: {"field": "availability", "value": "available"},
        ActionType.NOTE_APPEND: {"text": "test note"},
        ActionType.ROW_MOVE: {"destination": "completed"},
        ActionType.ALTERNATE_PROPERTY_PROPOSAL: {"summary": "alternate"},
        ActionType.FOLLOWUP_FREEZE: {"reason": "opt_out"},
        ActionType.STATUS_TRANSITION: {"status": "review"},
        ActionType.NOTIFICATION: {"message": "review needed"},
        ActionType.REVIEW_ITEM: {"summary": "review needed"},
        ActionType.RECIPIENT_CHANGE: {"reason": "broker referral"},
        ActionType.TOUR_REQUEST: {"notes": "tour requested"},
        ActionType.CALL_REQUEST: {"notes": "call requested"},
        ActionType.LOI_REQUEST: {"notes": "LOI requested"},
        ActionType.OUTBOUND_DRAFT: {"subject": "Re: property", "body": "Thanks"},
    }[action_type]
    return PlannedAction.create(
        tenant_id=decision.tenant_id,
        client_id="client-1",
        campaign_id=decision.campaign_id,
        thread_id=thread_id,
        sheet_id=sheet_id,
        row_anchor=row_anchor,
        decision_id=decision.decision_id,
        contract_id=decision.contract_id,
        action_type=action_type,
        approval_class=approval_class,
        target_entity_id=target_entity_id or decision.entity_id,
        contract_version=decision.contract_version,
        snapshot_hash=decision.snapshot_hash,
        source_claim_ids=source_claim_ids,
        operation_key=operation_key or f"message-1:{action_type.value}",
        expected_prior_state={"rowAnchor": "123 Industrial Ave|Suite B"},
        dependencies=dependencies,
        sequence=sequence,
        recipient=recipient,
        payload=payload or default_payload,
        reason="test_reason",
    )


def _plan(decision, actions):
    return ActionPlan.create(
        tenant_id=decision.tenant_id,
        client_id=decision.client_id,
        campaign_id=decision.campaign_id,
        decision_id=decision.decision_id,
        contract_id=decision.contract_id,
        contract_version=decision.contract_version,
        snapshot_hash=decision.snapshot_hash,
        actions=tuple(actions),
    )


def _scope(decision):
    return ExecutionScope(
        tenant_id=decision.tenant_id,
        client_id=decision.client_id,
        campaign_id=decision.campaign_id,
        thread_id="thread-1",
        sheet_id="sheet-1",
        row_anchor="123 Industrial Ave|Suite B",
    )


class ClaimPipelineValidationTests(unittest.TestCase):
    def test_terminal_decision_allows_supported_followup_freeze(self):
        entity = _entity()
        contract = _contract()
        decision = DecisionSnapshot.create(
            tenant_id=entity.tenant_id,
            client_id=contract.client_id,
            campaign_id=contract.campaign_id,
            contract_id=contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=contract.version,
            snapshot_hash="snapshot-1",
            market_state=MarketState.UNAVAILABLE,
            fit_state=FitState.NONVIABLE,
            completeness_state=CompletenessState.NOT_APPLICABLE,
            conversation_state=ConversationState.TERMINAL_INTENT,
            reason_codes=("broker_confirmed_unavailable",),
        )
        source = _source_claim(entity)
        action = _action(
            decision,
            ActionType.FOLLOWUP_FREEZE,
            ApprovalClass.AUTOMATIC,
            source_claim_ids=(source.claim_id,),
            payload={"reason": "broker_confirmed_unavailable"},
        )

        validate_action_plan(
            _plan(decision, (action,)),
            decision,
            scope=_scope(decision),
            entities=(entity,),
            claims=(source,),
            authorized_recipients=(),
        )

    def test_nonterminal_decision_rejects_followup_freeze(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        source = _source_claim(entity)
        action = _action(
            decision,
            ActionType.FOLLOWUP_FREEZE,
            ApprovalClass.AUTOMATIC,
            source_claim_ids=(source.claim_id,),
            payload={"reason": "broker_confirmed_unavailable"},
        )

        with self.assertRaisesRegex(ContractViolation, "terminal intent"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(source,),
                authorized_recipients=(),
            )

    def test_claim_bundle_rejects_unknown_evidence(self):
        evidence = _evidence()
        entity = _entity()
        claim = _claim(evidence, entity)

        with self.assertRaisesRegex(ContractViolation, "unknown evidence"):
            validate_claim_bundle(
                tenant_id="uid-1",
                evidence=(),
                entities=(entity,),
                claims=(claim,),
            )

    def test_claim_bundle_rejects_evidence_excerpt_not_present_in_source(self):
        evidence = _evidence()
        entity = _entity()
        claim = _claim(evidence, entity, evidence_text="Suite A is leased.")

        with self.assertRaisesRegex(ContractViolation, "evidence excerpt"):
            validate_claim_bundle(
                tenant_id="uid-1",
                evidence=(evidence,),
                entities=(entity,),
                claims=(claim,),
            )

    def test_claim_bundle_rejects_cross_tenant_entity(self):
        evidence = _evidence()
        entity = _entity(tenant_id="uid-2")
        claim = _claim(evidence, entity)

        with self.assertRaisesRegex(ContractViolation, "tenant"):
            validate_claim_bundle(
                tenant_id="uid-1",
                evidence=(evidence,),
                entities=(entity,),
                claims=(claim,),
            )

    def test_evidence_rejects_content_changed_under_existing_identity(self):
        evidence = _evidence()

        with self.assertRaisesRegex(ValueError, "content hash"):
            replace(evidence, content="Suite A is leased.")

    def test_claim_bundle_rejects_actor_authority_laundering(self):
        evidence = _evidence()
        entity = _entity()
        claim = Claim.create(
            tenant_id=evidence.tenant_id,
            evidence_id=evidence.evidence_id,
            subject_entity_id=entity.entity_id,
            predicate=ClaimPredicate.AVAILABILITY,
            value="available",
            evidence_text="Suite B is available now.",
            actor_role=ActorRole.USER,
            polarity=ClaimPolarity.POSITIVE,
            modality=ClaimModality.ASSERTED,
            confidence=0.98,
        )

        with self.assertRaisesRegex(ContractViolation, "actor role"):
            validate_claim_bundle(
                tenant_id="uid-1",
                evidence=(evidence,),
                entities=(entity,),
                claims=(claim,),
            )

    def test_valid_claim_bundle_passes(self):
        evidence = _evidence()
        entity = _entity()
        claim = _claim(evidence, entity)

        validate_claim_bundle(
            tenant_id="uid-1",
            evidence=(evidence,),
            entities=(entity,),
            claims=(claim,),
        )

    def test_decision_rejects_stale_contract_version(self):
        entity = _entity()
        current_contract = _contract(version=3)
        stale_decision = _decision(entity, _contract(version=2))

        with self.assertRaisesRegex(ContractViolation, "contract version"):
            validate_decision(
                stale_decision,
                entities=(entity,),
                contract=current_contract,
            )

    def test_action_plan_rejects_automatic_new_recipient(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.RECIPIENT_CHANGE,
            ApprovalClass.AUTOMATIC,
        )

        with self.assertRaisesRegex(ContractViolation, "requires human approval"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=("broker@example.com",),
            )

    def test_action_plan_rejects_stale_snapshot(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = PlannedAction.create(
            tenant_id=decision.tenant_id,
            client_id="client-1",
            campaign_id=decision.campaign_id,
            thread_id="thread-1",
            sheet_id="sheet-1",
            row_anchor="123 Industrial Ave|Suite B",
            decision_id=decision.decision_id,
            contract_id=decision.contract_id,
            action_type=ActionType.FACT_UPDATE,
            approval_class=ApprovalClass.AUTOMATIC,
            target_entity_id=decision.entity_id,
            contract_version=decision.contract_version,
            snapshot_hash="stale-snapshot",
            source_claim_ids=(_source_claim(entity).claim_id,),
            operation_key="message-1:availability",
            expected_prior_state={"availability": ""},
            dependencies=(),
            sequence=1,
            recipient="",
            payload={"field": "availability", "value": "available"},
            reason="broker_confirmed_available",
        )

        with self.assertRaisesRegex(ContractViolation, "snapshot"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=("broker@example.com",),
            )

    def test_compound_plan_allows_automatic_fact_and_reviewed_call(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        actions = (
            _action(
                decision,
                ActionType.FACT_UPDATE,
                ApprovalClass.AUTOMATIC,
                sequence=1,
            ),
            _action(
                decision,
                ActionType.CALL_REQUEST,
                ApprovalClass.HUMAN_REQUIRED,
                sequence=2,
            ),
        )

        validate_action_plan(
            _plan(decision, actions),
            decision,
            scope=_scope(decision),
            entities=(entity,),
            claims=(
                _source_claim(entity),
                _source_claim(entity, ClaimPredicate.CALL_REQUEST),
            ),
            authorized_recipients=("broker@example.com",),
        )

    def test_decision_rejects_same_version_contract_from_another_campaign(self):
        entity = _entity()
        campaign_a = _contract(version=1)
        campaign_b = CampaignContract.create(
            tenant_id="uid-1",
            client_id="client-2",
            campaign_id="campaign-2",
            version=1,
        )
        decision = _decision(entity, campaign_a)

        with self.assertRaisesRegex(ContractViolation, "campaign"):
            validate_decision(decision, entities=(entity,), contract=campaign_b)

    def test_automatic_action_cannot_target_a_different_entity(self):
        entity = _entity()
        alternate = EntityRef.create(
            tenant_id="uid-1",
            campaign_id="campaign-1",
            entity_type=EntityType.PROPERTY,
            label="Alternate Property",
            canonical_address="900 Replacement Rd",
            relationship="alternate",
        )
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            target_entity_id=alternate.entity_id,
        )

        with self.assertRaisesRegex(ContractViolation, "automatic action target"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity, alternate),
                claims=(_source_claim(entity),),
                authorized_recipients=("broker@example.com",),
            )

    def test_outbound_draft_to_new_recipient_requires_human_approval(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.OUTBOUND_DRAFT,
            ApprovalClass.AUTOMATIC,
            recipient="new-contact@example.com",
        )

        with self.assertRaisesRegex(ContractViolation, "new recipient"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=("broker@example.com",),
            )

    def test_action_plan_rejects_cross_campaign_scope(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
        )
        scope = replace(_scope(decision), campaign_id="campaign-2")

        with self.assertRaisesRegex(ContractViolation, "campaign"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=scope,
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )

    def test_action_plan_rejects_cross_client_action(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        with self.assertRaisesRegex(ValueError, "action identity"):
            replace(
                _action(decision, ActionType.FACT_UPDATE, ApprovalClass.AUTOMATIC),
                client_id="client-2",
            )

    def test_action_plan_rejects_unknown_dependency(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            dependencies=("action-missing",),
        )

        with self.assertRaisesRegex(ContractViolation, "unknown action"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )

    def test_action_plan_rejects_dependency_that_does_not_precede_action(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        second = _action(
            decision,
            ActionType.NOTE_APPEND,
            ApprovalClass.AUTOMATIC,
            sequence=2,
        )
        first = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            sequence=1,
            dependencies=(second.action_id,),
        )

        with self.assertRaisesRegex(ContractViolation, "must precede"):
            validate_action_plan(
                _plan(decision, (first, second)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )

    def test_outbound_draft_to_authorized_recipient_can_be_automatic(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.OUTBOUND_DRAFT,
            ApprovalClass.AUTOMATIC,
            recipient="broker@example.com",
        )

        validate_action_plan(
            _plan(decision, (action,)),
            decision,
            scope=_scope(decision),
            entities=(entity,),
            claims=(_source_claim(entity),),
            authorized_recipients=("Broker@Example.com",),
        )

    def test_action_must_match_authoritative_thread_sheet_and_row_scope(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            thread_id="unrelated-thread",
            sheet_id="unrelated-sheet",
            row_anchor="900 Wrong Property Rd",
        )

        with self.assertRaisesRegex(ContractViolation, "execution scope"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )

    def test_action_rejects_unknown_source_claim(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            source_claim_ids=("claim-missing",),
        )

        with self.assertRaisesRegex(ContractViolation, "unknown source claim"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )

    def test_outbound_payload_cannot_hide_a_different_recipient(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.OUTBOUND_DRAFT,
            ApprovalClass.AUTOMATIC,
            recipient="broker@example.com",
            payload={
                "toRecipients": [
                    {"emailAddress": {"address": "new-contact@example.com"}}
                ]
            },
        )

        with self.assertRaisesRegex(ContractViolation, "payload has forbidden keys"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=("broker@example.com",),
            )

    def test_availability_claim_cannot_authorize_a_rent_update(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            payload={"field": "rent", "value": 15.0},
        )

        with self.assertRaisesRegex(ContractViolation, "destination field"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )

    def test_operation_label_cannot_create_a_second_semantic_effect(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        first = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            sequence=1,
            operation_key="caller-label-one",
        )
        second = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.AUTOMATIC,
            sequence=2,
            operation_key="caller-label-two",
        )

        self.assertEqual(first.idempotency_key, second.idempotency_key)
        with self.assertRaisesRegex(ContractViolation, "idempotency key"):
            validate_action_plan(
                _plan(decision, (first, second)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )

    def test_forbidden_action_cannot_enter_a_plan(self):
        entity = _entity()
        decision = _decision(entity, _contract())
        action = _action(
            decision,
            ActionType.FACT_UPDATE,
            ApprovalClass.FORBIDDEN,
        )

        with self.assertRaisesRegex(ContractViolation, "forbidden"):
            validate_action_plan(
                _plan(decision, (action,)),
                decision,
                scope=_scope(decision),
                entities=(entity,),
                claims=(_source_claim(entity),),
                authorized_recipients=(),
            )


if __name__ == "__main__":
    unittest.main()
