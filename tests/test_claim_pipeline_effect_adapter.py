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
    OUTBOUND_ACTION_TYPES,
    SUPPORTED_ACTION_TYPES,
    TERMINAL_STATES,
    evaluate_effect_plan,
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


_ACTION_FIXTURES = {
    ActionType.FACT_UPDATE: (
        ClaimPredicate.AVAILABILITY,
        {"field": "availability", "value": "available", "confidence": 0.99},
        {"availability": "unknown"},
        "",
    ),
    ActionType.FOLLOWUP_FREEZE: (
        ClaimPredicate.OPT_OUT,
        {"reason": "contact_opt_out"},
        {"followUpStatus": "waiting"},
        "",
    ),
    ActionType.STATUS_TRANSITION: (
        ClaimPredicate.AVAILABILITY,
        {"status": "waiting_user"},
        {"conversationState": "active"},
        "",
    ),
    ActionType.ALTERNATE_PROPERTY_PROPOSAL: (
        ClaimPredicate.IDENTITY,
        {"summary": "alternate-fixture"},
        {},
        "",
    ),
    ActionType.RECIPIENT_CHANGE: (
        ClaimPredicate.REFERRAL,
        {"reason": "redirect_requires_approval"},
        {"recipient": ""},
        "recipient-fixture",
    ),
    ActionType.CALL_REQUEST: (
        ClaimPredicate.CALL_REQUEST,
        {"notes": "call-fixture", "phone": ""},
        {},
        "",
    ),
    ActionType.TOUR_REQUEST: (
        ClaimPredicate.TOUR_REQUEST,
        {"notes": "tour-fixture"},
        {},
        "",
    ),
    ActionType.INFORMATION_REQUEST: (
        ClaimPredicate.INFORMATION_REQUEST,
        {"notes": "information-fixture"},
        {},
        "",
    ),
    ActionType.REVIEW_ITEM: (
        ClaimPredicate.AVAILABILITY,
        {
            "summary": "review-fixture",
            "details": {"reasonCodes": ["fixture-review"]},
        },
        {},
        "",
    ),
    ActionType.NOTE_APPEND: (
        ClaimPredicate.AVAILABILITY,
        {"text": "note-fixture"},
        {"note": ""},
        "",
    ),
    ActionType.ROW_MOVE: (
        ClaimPredicate.AVAILABILITY,
        {"destination": "nonviable-fixture"},
        {"rowState": "active"},
        "",
    ),
    ActionType.NOTIFICATION: (
        ClaimPredicate.AVAILABILITY,
        {"message": "notification-fixture"},
        {},
        "",
    ),
    ActionType.LOI_REQUEST: (
        ClaimPredicate.AVAILABILITY,
        {"notes": "loi-fixture", "terms": {}},
        {},
        "",
    ),
    ActionType.OUTBOUND_DRAFT: (
        ClaimPredicate.AVAILABILITY,
        {"subject": "subject-fixture", "body": "body-fixture"},
        {},
        "recipient-fixture",
    ),
}


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
    tenant_id = "tenant-fixture"
    client_id = "client-fixture"
    campaign_id = "campaign-fixture"
    snapshot_hash = "snapshot-fixture"
    contract = CampaignContract.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        version=1,
        transaction_types=("transaction-fixture",),
        required_fields=("availability",),
        source_authority=ContractAuthority.SETUP,
    )
    scope = ExecutionScope(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        thread_id="thread-fixture",
        sheet_id="sheet-fixture",
        row_anchor="row-fixture",
    )
    target = EntityRef.create(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        entity_type=EntityType.PROPERTY,
        label="target-fixture",
    )
    action_target = target
    entities = [target]
    if action_type is ActionType.ALTERNATE_PROPERTY_PROPOSAL:
        action_target = EntityRef.create(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            entity_type=EntityType.PROPERTY,
            label="alternate-fixture",
            relationship="alternate",
        )
        entities.append(action_target)

    predicate, payload, expected_prior_state, recipient = _ACTION_FIXTURES[action_type]
    claim_value = "available"
    if predicate is ClaimPredicate.REFERRAL:
        claim_value = {
            "name": "recipient-fixture",
            "email": "recipient-fixture",
        }
    elif predicate is ClaimPredicate.IDENTITY:
        claim_value = "alternate-fixture"
    elif predicate is ClaimPredicate.OPT_OUT:
        claim_value = True
    elif predicate in {
        ClaimPredicate.CALL_REQUEST,
        ClaimPredicate.TOUR_REQUEST,
        ClaimPredicate.INFORMATION_REQUEST,
    }:
        claim_value = f"{predicate.value}-fixture"

    claim = Claim.create(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        evidence_id=f"evidence-{action_type.value}-fixture",
        subject_entity_id=action_target.entity_id,
        predicate=predicate,
        value=claim_value,
        evidence_text=f"evidence-{action_type.value}-fixture",
        actor_role=ActorRole.BROKER,
        polarity=ClaimPolarity.POSITIVE,
        modality=ClaimModality.ASSERTED,
        confidence=0.99,
    )

    effective_conversation_state = conversation_state
    reason_codes = ()
    if action_type is ActionType.FOLLOWUP_FREEZE:
        effective_conversation_state = ConversationState.TERMINAL_INTENT
        reason_codes = ("contact_opt_out",)
    elif action_type is ActionType.REVIEW_ITEM:
        effective_conversation_state = ConversationState.REVIEW
        reason_codes = ("fixture-review",)

    decision = DecisionSnapshot.create(
        tenant_id=tenant_id,
        client_id=client_id,
        campaign_id=campaign_id,
        contract_id=contract.contract_id,
        entity_id=target.entity_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        market_state=MarketState.AVAILABLE,
        fit_state=FitState.VIABLE,
        completeness_state=CompletenessState.INCOMPLETE,
        conversation_state=effective_conversation_state,
        reason_codes=reason_codes,
        evidence_ids=(claim.evidence_id,),
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
        action_type=action_type,
        approval_class=approval_class,
        target_entity_id=action_target.entity_id,
        contract_version=contract.version,
        snapshot_hash=snapshot_hash,
        source_claim_ids=(claim.claim_id,),
        operation_key=f"operation-{action_type.value}-fixture",
        expected_prior_state=expected_prior_state,
        dependencies=dependencies,
        sequence=1,
        recipient=recipient,
        payload=payload,
        reason=(
            "contact_opt_out"
            if action_type is ActionType.FOLLOWUP_FREEZE
            else "reason-fixture"
        ),
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
    return EffectAdapterRequest.create(
        plan=plan,
        decision=decision,
        scope=scope,
        entities=tuple(entities),
        claims=(claim,),
        authorized_recipients=((recipient,) if recipient else ()),
        current_snapshot_hash=(
            snapshot_hash
            if current_snapshot_hash is None
            else current_snapshot_hash
        ),
        current_contract_id=(
            contract.contract_id
            if current_contract_id is None
            else current_contract_id
        ),
        current_contract_version=(
            contract.version
            if current_contract_version is None
            else current_contract_version
        ),
        current_states=(
            ActionStateSnapshot.create(
                action_id=action.action_id,
                values=(
                    expected_prior_state
                    if current_values is None
                    else current_values
                ),
            ),
        ),
        approval_grants=approval_grants,
        committed_idempotency_keys=committed_idempotency_keys,
    )


def _with_exact_approval(request: EffectAdapterRequest) -> EffectAdapterRequest:
    action = request.plan.actions[0]
    grant = ApprovalGrant.create(
        tenant_id=request.plan.tenant_id,
        plan_id=request.plan.plan_id,
        action_id=action.action_id,
        snapshot_hash=request.current_snapshot_hash,
        approved_by="operator-fixture",
    )
    return replace(request, approval_grants=(grant,))


def _two_action_dependency_request(
    first_state_matches: bool,
) -> EffectAdapterRequest:
    base = _request_fixture()
    first = base.plan.actions[0]
    second_claim = Claim.create(
        tenant_id=base.plan.tenant_id,
        campaign_id=base.plan.campaign_id,
        evidence_id="evidence-dependency-second-fixture",
        subject_entity_id=base.decision.entity_id,
        predicate=ClaimPredicate.AVAILABILITY,
        value="waiting_user",
        evidence_text="evidence-dependency-second-fixture",
        actor_role=ActorRole.BROKER,
        polarity=ClaimPolarity.POSITIVE,
        modality=ClaimModality.ASSERTED,
        confidence=0.99,
    )
    second = PlannedAction.create(
        tenant_id=first.tenant_id,
        client_id=first.client_id,
        campaign_id=first.campaign_id,
        thread_id=first.thread_id,
        sheet_id=first.sheet_id,
        row_anchor=first.row_anchor,
        decision_id=first.decision_id,
        contract_id=first.contract_id,
        action_type=ActionType.STATUS_TRANSITION,
        approval_class=ApprovalClass.AUTOMATIC,
        target_entity_id=first.target_entity_id,
        contract_version=first.contract_version,
        snapshot_hash=first.snapshot_hash,
        source_claim_ids=(second_claim.claim_id,),
        operation_key="operation-dependency-second-fixture",
        expected_prior_state={"conversationState": "active"},
        dependencies=(first.action_id,),
        sequence=2,
        recipient="",
        payload={"status": "waiting_user"},
        reason="reason-dependency-second-fixture",
    )
    plan = ActionPlan.create(
        tenant_id=base.plan.tenant_id,
        client_id=base.plan.client_id,
        campaign_id=base.plan.campaign_id,
        decision_id=base.plan.decision_id,
        contract_id=base.plan.contract_id,
        contract_version=base.plan.contract_version,
        snapshot_hash=base.plan.snapshot_hash,
        actions=(first, second),
    )
    grants = tuple(
        ApprovalGrant.create(
            tenant_id=plan.tenant_id,
            plan_id=plan.plan_id,
            action_id=action.action_id,
            snapshot_hash=plan.snapshot_hash,
            approved_by=f"operator-{index}-fixture",
        )
        for index, action in enumerate(plan.actions, start=1)
    )
    return EffectAdapterRequest.create(
        plan=plan,
        decision=base.decision,
        scope=base.scope,
        entities=base.entities,
        claims=(*base.claims, second_claim),
        authorized_recipients=base.authorized_recipients,
        current_snapshot_hash=base.current_snapshot_hash,
        current_contract_id=base.current_contract_id,
        current_contract_version=base.current_contract_version,
        current_states=(
            ActionStateSnapshot.create(
                action_id=first.action_id,
                values=(
                    first.expected_prior_state
                    if first_state_matches
                    else {"availability": "different"}
                ),
            ),
            ActionStateSnapshot.create(
                action_id=second.action_id,
                values=second.expected_prior_state,
            ),
        ),
        approval_grants=grants,
        committed_idempotency_keys=(
            "committed-first-fixture",
            "committed-second-fixture",
        ),
    )


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


class EffectAdapterEvaluationTests(unittest.TestCase):
    def assert_disposition(self, expected, request):
        receipt = evaluate_effect_plan(request).effects[0]
        self.assertEqual(expected, (receipt.status, receipt.reason))

    def test_public_action_sets_are_closed(self):
        self.assertEqual(
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
            },
            SUPPORTED_ACTION_TYPES,
        )
        self.assertEqual({ActionType.OUTBOUND_DRAFT}, OUTBOUND_ACTION_TYPES)
        self.assertEqual(
            {
                ConversationState.TERMINAL_INTENT,
                ConversationState.TERMINAL_PENDING_ACK,
                ConversationState.TERMINAL,
            },
            TERMINAL_STATES,
        )

    def test_matching_automatic_action_would_apply(self):
        self.assert_disposition(
            (
                DryRunStatus.WOULD_APPLY,
                DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
            ),
            _request_fixture(),
        )

    def test_stale_snapshot_blocks_every_action(self):
        self.assert_disposition(
            (DryRunStatus.BLOCKED, DryRunReason.STALE_SNAPSHOT),
            _request_fixture(current_snapshot_hash="stale-snapshot"),
        )

    def test_stale_contract_blocks_every_action(self):
        self.assert_disposition(
            (DryRunStatus.BLOCKED, DryRunReason.STALE_CONTRACT),
            _request_fixture(current_contract_version=2),
        )

    def test_prior_state_mismatch_blocks_action(self):
        self.assert_disposition(
            (DryRunStatus.BLOCKED, DryRunReason.PRIOR_STATE_MISMATCH),
            _request_fixture(current_values={"availability": "different"}),
        )

    def test_committed_idempotency_key_skips_action(self):
        request = _request_fixture()
        request = replace(
            request,
            committed_idempotency_keys=(
                request.plan.actions[0].idempotency_key,
            ),
        )
        self.assert_disposition(
            (
                DryRunStatus.SKIPPED,
                DryRunReason.IDEMPOTENCY_KEY_ALREADY_COMMITTED,
            ),
            request,
        )

    def test_human_action_without_approval_is_skipped(self):
        self.assert_disposition(
            (DryRunStatus.SKIPPED, DryRunReason.APPROVAL_REQUIRED),
            _request_fixture(
                action_type=ActionType.INFORMATION_REQUEST,
                approval_class=ApprovalClass.HUMAN_REQUIRED,
            ),
        )

    def test_exact_human_approval_would_apply(self):
        request = _request_fixture(
            action_type=ActionType.INFORMATION_REQUEST,
            approval_class=ApprovalClass.HUMAN_REQUIRED,
        )
        self.assert_disposition(
            (
                DryRunStatus.WOULD_APPLY,
                DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION,
            ),
            _with_exact_approval(request),
        )

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
                self.assert_disposition(
                    (DryRunStatus.SKIPPED, DryRunReason.APPROVAL_REQUIRED),
                    request,
                )
                self.assert_disposition(
                    (
                        DryRunStatus.WOULD_APPLY,
                        DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION,
                    ),
                    _with_exact_approval(request),
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
        self.assert_disposition(
            (DryRunStatus.BLOCKED, DryRunReason.APPROVAL_SCOPE_MISMATCH),
            request,
        )

    def test_terminal_outbound_draft_is_suppressed(self):
        self.assert_disposition(
            (
                DryRunStatus.BLOCKED,
                DryRunReason.TERMINAL_OUTBOUND_SUPPRESSED,
            ),
            _request_fixture(
                action_type=ActionType.OUTBOUND_DRAFT,
                approval_class=ApprovalClass.HUMAN_REQUIRED,
                conversation_state=ConversationState.TERMINAL_INTENT,
            ),
        )

    def test_unsupported_action_is_blocked(self):
        self.assert_disposition(
            (DryRunStatus.BLOCKED, DryRunReason.UNSUPPORTED_ACTION_TYPE),
            _request_fixture(action_type=ActionType.NOTIFICATION),
        )

    def test_blocked_dependency_blocks_dependent_action(self):
        commit = evaluate_effect_plan(
            _two_action_dependency_request(first_state_matches=False)
        )
        self.assertEqual(
            (
                DryRunStatus.BLOCKED,
                DryRunReason.PRIOR_STATE_MISMATCH,
            ),
            (commit.effects[0].status, commit.effects[0].reason),
        )
        self.assertEqual(
            (
                DryRunStatus.BLOCKED,
                DryRunReason.DEPENDENCY_BLOCKED,
            ),
            (commit.effects[1].status, commit.effects[1].reason),
        )
        self.assertEqual(
            (commit.effects[0].receipt_id,),
            commit.effects[1].dependency_receipt_ids,
        )
        self.assertNotEqual(
            (commit.effects[0].action_id,),
            commit.effects[1].dependency_receipt_ids,
        )

    def test_forbidden_action_blocks_the_whole_plan_as_contract_violation(self):
        commit = evaluate_effect_plan(
            _request_fixture(approval_class=ApprovalClass.FORBIDDEN)
        )
        self.assertTrue(commit.effects)
        self.assertTrue(
            all(
                (effect.status, effect.reason)
                == (
                    DryRunStatus.BLOCKED,
                    DryRunReason.PLAN_CONTRACT_VIOLATION,
                )
                for effect in commit.effects
            )
        )

    def test_repeated_and_reversed_inputs_are_byte_stable(self):
        request = _two_action_dependency_request(first_state_matches=True)
        forward = evaluate_effect_plan(request)
        repeated = evaluate_effect_plan(request)
        reversed_request = replace(
            request,
            current_states=tuple(reversed(request.current_states)),
            approval_grants=tuple(reversed(request.approval_grants)),
            committed_idempotency_keys=tuple(
                reversed(request.committed_idempotency_keys)
            ),
        )
        reversed_result = evaluate_effect_plan(reversed_request)

        self.assertEqual(forward.receipt_id, repeated.receipt_id)
        self.assertEqual(forward.receipt_id, reversed_result.receipt_id)
        self.assertEqual(
            json.dumps(forward.to_dict(), sort_keys=True),
            json.dumps(reversed_result.to_dict(), sort_keys=True),
        )


if __name__ == "__main__":
    unittest.main()
