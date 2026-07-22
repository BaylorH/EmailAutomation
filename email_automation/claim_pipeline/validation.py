"""Pure fail-closed validation for claim-pipeline contracts."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import (
    ActionPlan,
    ActionType,
    ApprovalClass,
    CampaignContract,
    Claim,
    ClaimPredicate,
    ConversationState,
    DecisionSnapshot,
    EntityRef,
    EvidenceEnvelope,
    ExecutionScope,
    PlannedAction,
)


class ContractViolation(ValueError):
    """Raised when pipeline contracts cannot be safely connected."""


APPROVAL_GATED_ACTION_TYPES = frozenset(
    {
        ActionType.RECIPIENT_CHANGE,
        ActionType.ALTERNATE_PROPERTY_PROPOSAL,
        ActionType.TOUR_REQUEST,
        ActionType.CALL_REQUEST,
        ActionType.LOI_REQUEST,
    }
)

STATE_MUTATING_ACTION_TYPES = frozenset(
    {
        ActionType.FACT_UPDATE,
        ActionType.NOTE_APPEND,
        ActionType.ROW_MOVE,
        ActionType.FOLLOWUP_FREEZE,
        ActionType.STATUS_TRANSITION,
        ActionType.RECIPIENT_CHANGE,
    }
)

ACTION_PAYLOAD_KEYS = {
    ActionType.FACT_UPDATE: frozenset({"field", "value", "unit", "confidence"}),
    ActionType.NOTE_APPEND: frozenset({"text"}),
    ActionType.ROW_MOVE: frozenset({"destination"}),
    ActionType.ALTERNATE_PROPERTY_PROPOSAL: frozenset({"summary"}),
    ActionType.FOLLOWUP_FREEZE: frozenset({"reason"}),
    ActionType.STATUS_TRANSITION: frozenset({"status"}),
    ActionType.NOTIFICATION: frozenset({"message"}),
    ActionType.REVIEW_ITEM: frozenset({"summary", "details"}),
    ActionType.RECIPIENT_CHANGE: frozenset({"reason"}),
    ActionType.TOUR_REQUEST: frozenset({"notes", "requested_times"}),
    ActionType.CALL_REQUEST: frozenset({"notes", "phone"}),
    ActionType.LOI_REQUEST: frozenset({"notes", "terms"}),
    ActionType.OUTBOUND_DRAFT: frozenset({"subject", "body", "html"}),
}

FACT_FIELD_PREDICATES = {
    predicate.value: predicate
    for predicate in (
        ClaimPredicate.AVAILABILITY,
        ClaimPredicate.ASKING_STATUS,
        ClaimPredicate.TRANSACTION_TYPE,
        ClaimPredicate.TOTAL_SF,
        ClaimPredicate.OFFICE_SF,
        ClaimPredicate.RENT,
        ClaimPredicate.OPERATING_EXPENSES,
        ClaimPredicate.POWER,
        ClaimPredicate.CLEAR_HEIGHT,
        ClaimPredicate.DRIVE_INS,
        ClaimPredicate.DOCKS,
        ClaimPredicate.OCCUPANCY_DATE,
        ClaimPredicate.TERM,
    )
}

ACTION_SUPPORT_PREDICATES = {
    ActionType.RECIPIENT_CHANGE: frozenset({ClaimPredicate.REFERRAL}),
    ActionType.ALTERNATE_PROPERTY_PROPOSAL: frozenset({ClaimPredicate.IDENTITY}),
    ActionType.TOUR_REQUEST: frozenset({ClaimPredicate.TOUR_REQUEST}),
    ActionType.CALL_REQUEST: frozenset({ClaimPredicate.CALL_REQUEST}),
}


def _unique_by_id(items: Iterable[object], attribute: str, label: str) -> dict[str, object]:
    indexed = {}
    for item in items:
        item_id = str(getattr(item, attribute, "") or "").strip()
        if not item_id:
            raise ContractViolation(f"{label} has no {attribute}")
        if item_id in indexed:
            raise ContractViolation(f"duplicate {label} {item_id}")
        indexed[item_id] = item
    return indexed


def _require_tenant(expected: str, actual: str, label: str) -> None:
    if actual != expected:
        raise ContractViolation(
            f"{label} tenant {actual!r} does not match expected tenant {expected!r}"
        )


def _validate_action_payload(
    action: PlannedAction,
    source_claims: tuple[Claim, ...],
    decision: DecisionSnapshot,
) -> None:
    allowed_keys = ACTION_PAYLOAD_KEYS[action.action_type]
    unknown_keys = set(action.payload) - allowed_keys
    if unknown_keys:
        raise ContractViolation(
            f"{action.action_type.value} payload has forbidden keys: "
            f"{sorted(unknown_keys)}"
        )

    source_predicates = {claim.predicate for claim in source_claims}
    if action.action_type is ActionType.FACT_UPDATE:
        field = str(action.payload.get("field", "") or "").strip().lower()
        expected_predicate = FACT_FIELD_PREDICATES.get(field)
        if expected_predicate is None:
            raise ContractViolation("fact update payload has unsupported or missing field")
        if "value" not in action.payload:
            raise ContractViolation("fact update payload requires a value")
        if expected_predicate not in source_predicates:
            raise ContractViolation(
                "fact update source claims do not support the destination field"
            )

    supported_predicates = ACTION_SUPPORT_PREDICATES.get(action.action_type)
    if supported_predicates and source_predicates.isdisjoint(supported_predicates):
        raise ContractViolation(
            f"{action.action_type.value} source claims do not support the action"
        )

    if action.action_type is ActionType.FOLLOWUP_FREEZE:
        reason = str(action.payload.get("reason", "") or "").strip()
        terminal_support = frozenset(
            {
                ClaimPredicate.AVAILABILITY,
                ClaimPredicate.OCCUPANCY_DATE,
                ClaimPredicate.TERM,
                ClaimPredicate.DRIVE_INS,
                ClaimPredicate.OPT_OUT,
                ClaimPredicate.TOTAL_SF,
                ClaimPredicate.RENT,
                ClaimPredicate.OPERATING_EXPENSES,
            }
        )
        if decision.conversation_state is not ConversationState.TERMINAL_INTENT:
            raise ContractViolation("followup freeze requires terminal intent")
        if source_predicates.isdisjoint(terminal_support):
            raise ContractViolation(
                "followup freeze source claims do not support terminal intent"
            )
        if reason not in decision.reason_codes:
            raise ContractViolation(
                "followup freeze reason does not match the terminal decision"
            )


def validate_claim_bundle(
    *,
    tenant_id: str,
    evidence: Iterable[EvidenceEnvelope],
    entities: Iterable[EntityRef],
    claims: Iterable[Claim],
    known_claim_ids: Iterable[str] = (),
) -> None:
    """Validate provenance and subject bindings without performing side effects."""
    evidence_by_id = _unique_by_id(evidence, "evidence_id", "evidence")
    entities_by_id = _unique_by_id(entities, "entity_id", "entity")
    claims_by_id = _unique_by_id(claims, "claim_id", "claim")
    known_ids = {
        str(claim_id or "").strip()
        for claim_id in known_claim_ids
        if str(claim_id or "").strip()
    }

    for envelope in evidence_by_id.values():
        _require_tenant(tenant_id, envelope.tenant_id, "evidence")
        if envelope.parent_evidence_id and envelope.parent_evidence_id not in evidence_by_id:
            raise ContractViolation(
                f"evidence {envelope.evidence_id} references unknown parent evidence"
            )

    for entity in entities_by_id.values():
        _require_tenant(tenant_id, entity.tenant_id, "entity")
        for evidence_id in entity.evidence_ids:
            if evidence_id not in evidence_by_id:
                raise ContractViolation(
                    f"entity {entity.entity_id} references unknown evidence {evidence_id}"
                )

    for claim in claims_by_id.values():
        _require_tenant(tenant_id, claim.tenant_id, "claim")
        envelope = evidence_by_id.get(claim.evidence_id)
        if envelope is None:
            raise ContractViolation(
                f"claim {claim.claim_id} references unknown evidence {claim.evidence_id}"
            )
        if claim.subject_entity_id not in entities_by_id:
            raise ContractViolation(
                f"claim {claim.claim_id} references unknown entity {claim.subject_entity_id}"
            )
        if claim.evidence_text.strip() not in envelope.content:
            raise ContractViolation(
                f"claim {claim.claim_id} evidence excerpt is not present in its source"
            )
        if claim.actor_role is not envelope.actor.role:
            raise ContractViolation(
                f"claim {claim.claim_id} actor role does not match evidence actor"
            )
        if claim.campaign_id and claim.campaign_id != envelope.campaign_id:
            raise ContractViolation(
                f"claim {claim.claim_id} campaign does not match evidence campaign"
            )
        if claim.actor_email and claim.actor_email != envelope.actor.email.strip().casefold():
            raise ContractViolation(
                f"claim {claim.claim_id} actor identity does not match evidence actor"
            )
        if claim.observed_at and claim.observed_at != envelope.observed_at:
            raise ContractViolation(
                f"claim {claim.claim_id} chronology does not match evidence chronology"
            )
        if claim.supersedes_claim_id:
            if claim.supersedes_claim_id == claim.claim_id:
                raise ContractViolation(f"claim {claim.claim_id} cannot supersede itself")
            if (
                claim.supersedes_claim_id not in claims_by_id
                and claim.supersedes_claim_id not in known_ids
            ):
                raise ContractViolation(
                    f"claim {claim.claim_id} supersedes unknown claim "
                    f"{claim.supersedes_claim_id}"
                )


def validate_decision(
    decision: DecisionSnapshot,
    *,
    entities: Iterable[EntityRef],
    contract: CampaignContract,
) -> None:
    """Require a current tenant, entity, and campaign-contract binding."""
    entities_by_id = _unique_by_id(entities, "entity_id", "entity")
    _require_tenant(contract.tenant_id, decision.tenant_id, "decision")
    if decision.client_id != contract.client_id:
        raise ContractViolation("decision client does not match campaign contract")
    if decision.campaign_id != contract.campaign_id:
        raise ContractViolation("decision campaign does not match campaign contract")
    if decision.contract_version != contract.version:
        raise ContractViolation(
            "decision contract version does not match the effective campaign contract"
        )
    if decision.contract_id != contract.contract_id:
        raise ContractViolation("decision contract identity does not match campaign contract")
    entity = entities_by_id.get(decision.entity_id)
    if entity is None:
        raise ContractViolation(f"decision references unknown entity {decision.entity_id}")
    _require_tenant(contract.tenant_id, entity.tenant_id, "decision entity")
    if entity.campaign_id != contract.campaign_id:
        raise ContractViolation("decision entity campaign does not match campaign contract")


def validate_action_plan(
    plan: ActionPlan,
    decision: DecisionSnapshot,
    *,
    scope: ExecutionScope,
    entities: Iterable[EntityRef],
    claims: Iterable[Claim],
    authorized_recipients: Iterable[str],
) -> None:
    """Require current decision bindings and approval for sensitive effects."""
    entities_by_id = _unique_by_id(entities, "entity_id", "entity")
    claims_by_id = _unique_by_id(claims, "claim_id", "claim")
    authorized = {
        str(recipient or "").strip().lower()
        for recipient in authorized_recipients
        if str(recipient or "").strip()
    }
    _require_tenant(decision.tenant_id, plan.tenant_id, "action plan")
    _require_tenant(decision.tenant_id, scope.tenant_id, "execution scope")
    if scope.client_id != decision.client_id:
        raise ContractViolation("execution scope client does not match decision snapshot")
    if scope.campaign_id != decision.campaign_id:
        raise ContractViolation("execution scope campaign does not match decision snapshot")
    if plan.client_id != decision.client_id:
        raise ContractViolation("action plan client does not match decision snapshot")
    if plan.campaign_id != decision.campaign_id:
        raise ContractViolation("action plan campaign does not match decision snapshot")
    if plan.decision_id != decision.decision_id:
        raise ContractViolation("action plan decision does not match decision snapshot")
    if plan.contract_id != decision.contract_id:
        raise ContractViolation("action plan contract does not match decision snapshot")
    if plan.contract_version != decision.contract_version:
        raise ContractViolation("action plan contract version is stale")
    if plan.snapshot_hash != decision.snapshot_hash:
        raise ContractViolation("action plan snapshot does not match decision snapshot")

    action_ids = set()
    idempotency_keys = set()
    sequences = set()
    for action in plan.actions:
        _require_tenant(decision.tenant_id, action.tenant_id, "planned action")
        if action.client_id != decision.client_id:
            raise ContractViolation("planned action client does not match snapshot")
        if action.campaign_id != decision.campaign_id:
            raise ContractViolation("planned action campaign does not match snapshot")
        if action.contract_id != decision.contract_id:
            raise ContractViolation("planned action contract does not match snapshot")
        for label, actual, expected in (
            ("thread", action.thread_id, scope.thread_id),
            ("sheet", action.sheet_id, scope.sheet_id),
            ("row anchor", action.row_anchor, scope.row_anchor),
        ):
            if actual != expected:
                raise ContractViolation(
                    f"planned action {label} does not match execution scope"
                )
        if action.action_id in action_ids:
            raise ContractViolation(f"duplicate action ID {action.action_id}")
        if action.idempotency_key in idempotency_keys:
            raise ContractViolation(
                f"duplicate action idempotency key {action.idempotency_key}"
            )
        action_ids.add(action.action_id)
        idempotency_keys.add(action.idempotency_key)
        if action.sequence in sequences:
            raise ContractViolation(f"duplicate action sequence {action.sequence}")
        sequences.add(action.sequence)

        if action.decision_id != decision.decision_id:
            raise ContractViolation("planned action decision does not match snapshot")
        if action.contract_version != decision.contract_version:
            raise ContractViolation("planned action contract version is stale")
        if action.snapshot_hash != decision.snapshot_hash:
            raise ContractViolation("planned action snapshot is stale")
        target = entities_by_id.get(action.target_entity_id)
        if target is None:
            raise ContractViolation(
                f"planned action references unknown entity {action.target_entity_id}"
            )
        _require_tenant(decision.tenant_id, target.tenant_id, "action target entity")
        if target.campaign_id != decision.campaign_id:
            raise ContractViolation("planned action target campaign does not match snapshot")
        if (
            action.approval_class is ApprovalClass.AUTOMATIC
            and action.target_entity_id != decision.entity_id
        ):
            raise ContractViolation(
                "automatic action target must match the decision entity"
            )
        if action.approval_class is ApprovalClass.FORBIDDEN:
            raise ContractViolation(f"{action.action_type.value} action is forbidden")
        if (
            action.action_type in APPROVAL_GATED_ACTION_TYPES
            and action.approval_class is not ApprovalClass.HUMAN_REQUIRED
        ):
            raise ContractViolation(
                f"{action.action_type.value} requires human approval"
            )
        if action.action_type is ActionType.OUTBOUND_DRAFT:
            if not action.recipient:
                raise ContractViolation("outbound draft requires a recipient")
            if (
                action.recipient not in authorized
                and action.approval_class is not ApprovalClass.HUMAN_REQUIRED
            ):
                raise ContractViolation(
                    "outbound draft to a new recipient requires human approval"
                )
        if not action.source_claim_ids:
            raise ContractViolation("planned action requires source claim IDs")
        if len(set(action.source_claim_ids)) != len(action.source_claim_ids):
            raise ContractViolation("planned action has duplicate source claim IDs")
        source_claims = []
        for claim_id in action.source_claim_ids:
            claim = claims_by_id.get(claim_id)
            if claim is None:
                raise ContractViolation(
                    f"planned action references unknown source claim {claim_id}"
                )
            _require_tenant(decision.tenant_id, claim.tenant_id, "source claim")
            if claim.subject_entity_id != action.target_entity_id:
                raise ContractViolation(
                    "planned action source claim does not support its target entity"
                )
            source_claims.append(claim)
        _validate_action_payload(action, tuple(source_claims), decision)
        if (
            action.action_type in STATE_MUTATING_ACTION_TYPES
            and not action.expected_prior_state
        ):
            raise ContractViolation(
                f"{action.action_type.value} requires expected prior state"
            )

    actions_by_id = {action.action_id: action for action in plan.actions}
    for action in plan.actions:
        for dependency_id in action.dependencies:
            dependency = actions_by_id.get(dependency_id)
            if dependency is None:
                raise ContractViolation(
                    f"planned action depends on unknown action {dependency_id}"
                )
            if dependency.action_id == action.action_id:
                raise ContractViolation("planned action cannot depend on itself")
            if dependency.sequence >= action.sequence:
                raise ContractViolation(
                    "planned action dependencies must precede dependent actions"
                )
