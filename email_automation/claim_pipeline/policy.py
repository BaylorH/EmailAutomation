"""Pure deterministic policy evaluation and no-effect action planning."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import (
    ActionPlan,
    ActionType,
    ApprovalClass,
    CampaignContract,
    Claim,
    ClaimPredicate,
    CompletenessState,
    ConversationState,
    DecisionSnapshot,
    EntityRef,
    EntityType,
    ExecutionScope,
    FitState,
    MarketState,
    PlannedAction,
)
from .validation import validate_action_plan, validate_decision


_FACT_PREDICATES = frozenset(
    {
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
    }
)
_SUPPORTED_HARD_REQUIREMENTS = frozenset(
    {"occupancy_by", "minimum_term_months", "drive_ins"}
)
_PROPERTY_ENTITY_TYPES = frozenset(
    {
        EntityType.TARGET_PROPERTY,
        EntityType.PROPERTY,
        EntityType.BUILDING,
        EntityType.SUITE,
    }
)
_TERMINAL_REASONS = (
    "contact_opted_out",
    "broker_confirmed_unavailable",
    "hard_occupancy_after_deadline",
    "hard_term_below_minimum",
    "hard_drive_ins_below_minimum",
    "required_facts_complete",
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _plain(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class PolicyEvaluationRequest:
    contract: CampaignContract
    scope: ExecutionScope
    entities: tuple[EntityRef, ...]
    claims: tuple[Claim, ...]
    snapshot_hash: str
    current_facts: Mapping[str, Mapping[str, Any]]
    current_conversation_states: Mapping[str, str]
    current_followup_states: Mapping[str, str]
    authorized_recipients: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        contract: CampaignContract,
        scope: ExecutionScope,
        entities: Iterable[EntityRef],
        claims: Iterable[Claim],
        snapshot_hash: str,
        current_facts: Mapping[str, Mapping[str, Any]] | None = None,
        current_conversation_states: Mapping[str, str] | None = None,
        current_followup_states: Mapping[str, str] | None = None,
        authorized_recipients: Iterable[str] = (),
    ) -> "PolicyEvaluationRequest":
        if not str(snapshot_hash or "").strip():
            raise ValueError("snapshot hash must be non-empty")
        if scope.tenant_id != contract.tenant_id:
            raise ValueError("execution scope tenant does not match contract")
        if scope.client_id != contract.client_id:
            raise ValueError("execution scope client does not match contract")
        if scope.campaign_id != contract.campaign_id:
            raise ValueError("execution scope campaign does not match contract")

        ordered_entities = tuple(sorted(tuple(entities), key=lambda item: item.entity_id))
        entity_ids = [item.entity_id for item in ordered_entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("duplicate policy entity")
        for entity in ordered_entities:
            if entity.tenant_id != contract.tenant_id:
                raise ValueError("policy entity tenant does not match contract")
            if entity.campaign_id != contract.campaign_id:
                raise ValueError("policy entity campaign does not match contract")

        ordered_claims = tuple(sorted(tuple(claims), key=lambda item: item.claim_id))
        claim_ids = [item.claim_id for item in ordered_claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("duplicate policy claim")
        claims_by_id = {item.claim_id: item for item in ordered_claims}
        entity_id_set = set(entity_ids)
        for claim in ordered_claims:
            if claim.tenant_id != contract.tenant_id:
                raise ValueError("policy claim tenant does not match contract")
            if claim.campaign_id and claim.campaign_id != contract.campaign_id:
                raise ValueError("policy claim campaign does not match contract")
            if claim.subject_entity_id not in entity_id_set:
                raise ValueError("policy claim references unknown entity")
            if claim.supersedes_claim_id:
                prior = claims_by_id.get(claim.supersedes_claim_id)
                if prior is None:
                    raise ValueError("policy correction supersedes unknown claim")
                if prior.subject_entity_id != claim.subject_entity_id:
                    raise ValueError("policy correction crosses entity scope")
                if (
                    prior.predicate is not claim.predicate
                    and claim.predicate is not ClaimPredicate.CORRECTION
                ):
                    raise ValueError("policy correction crosses predicate scope")

        def scoped_mapping(values: Mapping[str, Any] | None, label: str) -> Mapping[str, Any]:
            mapped = dict(values or {})
            unknown = set(mapped) - entity_id_set
            if unknown:
                raise ValueError(f"{label} references unknown entity")
            return _freeze(mapped)

        return cls(
            contract=contract,
            scope=scope,
            entities=ordered_entities,
            claims=ordered_claims,
            snapshot_hash=str(snapshot_hash).strip(),
            current_facts=scoped_mapping(current_facts, "current facts"),
            current_conversation_states=scoped_mapping(
                current_conversation_states, "current conversation states"
            ),
            current_followup_states=scoped_mapping(
                current_followup_states, "current followup states"
            ),
            authorized_recipients=tuple(
                sorted(
                    {
                        str(value or "").strip().casefold()
                        for value in authorized_recipients
                        if str(value or "").strip()
                    }
                )
            ),
        )


@dataclass(frozen=True)
class ClaimConflict:
    entity_id: str
    predicate: ClaimPredicate
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class EntityPolicyResult:
    decision: DecisionSnapshot
    approval_class: ApprovalClass
    action_plan: ActionPlan
    source_claim_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "approvalClass": self.approval_class.value,
            "actionPlan": self.action_plan.to_dict(),
            "sourceClaimIds": list(self.source_claim_ids),
        }


@dataclass(frozen=True)
class PolicyEvaluationResult:
    results: tuple[EntityPolicyResult, ...]
    conflicts: tuple[ClaimConflict, ...]
    result_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "results": [item.to_dict() for item in self.results],
            "conflicts": [
                {
                    "entityId": item.entity_id,
                    "predicate": item.predicate.value,
                    "claimIds": list(item.claim_ids),
                }
                for item in self.conflicts
            ],
            "resultDigest": self.result_digest,
        }


def _effective_claims(
    claims: tuple[Claim, ...],
) -> tuple[dict[str, dict[ClaimPredicate, tuple[Claim, ...]]], tuple[ClaimConflict, ...]]:
    superseded_ids = {
        claim.supersedes_claim_id
        for claim in claims
        if claim.supersedes_claim_id
    }
    grouped: dict[str, dict[ClaimPredicate, list[Claim]]] = {}
    for claim in claims:
        if claim.claim_id in superseded_ids:
            continue
        grouped.setdefault(claim.subject_entity_id, {}).setdefault(
            claim.predicate, []
        ).append(claim)

    effective: dict[str, dict[ClaimPredicate, tuple[Claim, ...]]] = {}
    conflicts = []
    for entity_id in sorted(grouped):
        effective[entity_id] = {}
        for predicate in sorted(grouped[entity_id], key=lambda item: item.value):
            candidates = tuple(sorted(grouped[entity_id][predicate], key=lambda item: item.claim_id))
            value_digests = {_canonical_digest(claim.value) for claim in candidates}
            if len(value_digests) > 1:
                conflicts.append(
                    ClaimConflict(
                        entity_id=entity_id,
                        predicate=predicate,
                        claim_ids=tuple(claim.claim_id for claim in candidates),
                    )
                )
            else:
                effective[entity_id][predicate] = candidates
    return effective, tuple(conflicts)


def _first_claim(
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    predicate: ClaimPredicate,
) -> Claim | None:
    values = claims.get(predicate, ())
    return values[0] if values else None


def _claim_value(
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    predicate: ClaimPredicate,
) -> Any:
    claim = _first_claim(claims, predicate)
    return claim.value if claim else None


def _referral_recipient(claim: Claim | None) -> str:
    if claim is None:
        return ""
    if isinstance(claim.value, Mapping):
        email = claim.value.get("email")
        return str(email).strip() if email else ""
    return str(claim.value).strip()


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _market_state(
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    *,
    has_conflict: bool,
) -> tuple[MarketState, list[str]]:
    if has_conflict:
        return MarketState.UNKNOWN, ["conflicting_active_claims", "market_state_unknown"]
    availability = str(_claim_value(claims, ClaimPredicate.AVAILABILITY) or "").casefold()
    if availability in {"unavailable", "leased", "off_market", "off market"}:
        return MarketState.UNAVAILABLE, ["broker_confirmed_unavailable"]
    if availability == "available":
        return MarketState.AVAILABLE, ["broker_confirmed_available"]
    asking = str(_claim_value(claims, ClaimPredicate.ASKING_STATUS) or "").casefold()
    if asking == "accepting_backups":
        return MarketState.CONDITIONAL, ["accepting_backup_offers"]
    remediation = _first_claim(claims, ClaimPredicate.REMEDIATION)
    if remediation and remediation.modality.value == "asserted":
        value = remediation.value
        if isinstance(value, Mapping) and value.get("funded") is True and value.get("by"):
            return MarketState.AVAILABLE, ["definite_remediation_before_deadline"]
    return MarketState.UNKNOWN, ["market_state_unknown"]


def _fit_state(
    request: PolicyEvaluationRequest,
    entity: EntityRef,
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    market: MarketState,
    reasons: list[str],
) -> tuple[FitState, list[str]]:
    if market is MarketState.UNAVAILABLE:
        return FitState.NONVIABLE, reasons

    unsupported = set(request.contract.hard_requirements) - _SUPPORTED_HARD_REQUIREMENTS
    if unsupported:
        return FitState.REVIEW, reasons + ["unsupported_hard_requirement"]

    occupancy_by = request.contract.hard_requirements.get("occupancy_by")
    remediation = _first_claim(claims, ClaimPredicate.REMEDIATION)
    if remediation:
        if remediation.modality.value != "asserted":
            return FitState.REVIEW, reasons + ["tentative_remediation_requires_review"]
        value = remediation.value
        by_value = value.get("by") if isinstance(value, Mapping) else None
        funded = value.get("funded") if isinstance(value, Mapping) else None
        remediated_drive_ins = (
            value.get("drive_ins") if isinstance(value, Mapping) else None
        )
        required_drive_ins = request.contract.hard_requirements.get("drive_ins")
        try:
            drive_ins_satisfied = required_drive_ins is None or (
                remediated_drive_ins is not None
                and float(remediated_drive_ins) >= float(required_drive_ins)
            )
        except (TypeError, ValueError):
            drive_ins_satisfied = False
        deadline = _as_date(occupancy_by) if occupancy_by else None
        remediation_date = _as_date(by_value) if by_value else None
        if funded is True and remediation_date and drive_ins_satisfied and (
            deadline is None or remediation_date <= deadline
        ):
            cleaned = [item for item in reasons if item != "market_state_unknown"]
            if "definite_remediation_before_deadline" not in cleaned:
                cleaned.append("definite_remediation_before_deadline")
            return FitState.CONDITIONAL, cleaned
        return FitState.REVIEW, reasons + ["tentative_remediation_requires_review"]

    occupancy = _claim_value(claims, ClaimPredicate.OCCUPANCY_DATE)
    if occupancy_by:
        if occupancy is None:
            return FitState.REVIEW, reasons + ["hard_requirement_unproven"]
        deadline = _as_date(occupancy_by)
        available_on = _as_date(occupancy)
        if deadline is None or available_on is None:
            return FitState.REVIEW, reasons + ["unsupported_hard_requirement"]
        if available_on > deadline:
            return FitState.NONVIABLE, reasons + ["hard_occupancy_after_deadline"]

    minimum_term = request.contract.hard_requirements.get("minimum_term_months")
    term = _claim_value(claims, ClaimPredicate.TERM)
    if minimum_term is not None:
        if term is None:
            return FitState.REVIEW, reasons + ["hard_requirement_unproven"]
        try:
            if float(term) < float(minimum_term):
                return FitState.NONVIABLE, reasons + ["hard_term_below_minimum"]
        except (TypeError, ValueError):
            return FitState.REVIEW, reasons + ["unsupported_hard_requirement"]

    required_drive_ins = request.contract.hard_requirements.get("drive_ins")
    if required_drive_ins is not None:
        drive_ins = _claim_value(claims, ClaimPredicate.DRIVE_INS)
        if drive_ins is None:
            return FitState.REVIEW, reasons + ["hard_requirement_unproven"]
        try:
            if float(drive_ins) < float(required_drive_ins):
                return FitState.NONVIABLE, reasons + ["hard_drive_ins_below_minimum"]
        except (TypeError, ValueError):
            return FitState.REVIEW, reasons + ["hard_requirement_unproven"]

    if market is MarketState.CONDITIONAL:
        return FitState.REVIEW, reasons
    if entity.relationship == "alternate":
        return FitState.REVIEW, reasons + ["alternate_property_requires_approval"]
    if _first_claim(claims, ClaimPredicate.REFERRAL):
        return FitState.REVIEW, reasons + ["redirect_requires_approval"]
    if market is MarketState.AVAILABLE:
        return FitState.VIABLE, reasons
    return FitState.REVIEW, reasons


def _approval_class(
    entity: EntityRef,
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    reasons: Iterable[str],
    conversation: ConversationState | None = None,
) -> ApprovalClass:
    reason_set = set(reasons)
    if (
        entity.relationship == "alternate"
        or _first_claim(claims, ClaimPredicate.REFERRAL)
        or _first_claim(claims, ClaimPredicate.CALL_REQUEST)
        or conversation is ConversationState.REVIEW
        or {
            "conflicting_active_claims",
            "unsupported_hard_requirement",
            "hard_requirement_unproven",
            "tentative_remediation_requires_review",
            "accepting_backup_offers",
        }
        & reason_set
    ):
        return ApprovalClass.HUMAN_REQUIRED
    return ApprovalClass.AUTOMATIC


def _completeness(
    request: PolicyEvaluationRequest,
    entity: EntityRef,
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    fit: FitState,
    approval: ApprovalClass,
) -> tuple[CompletenessState, tuple[str, ...], list[str]]:
    if fit is FitState.NONVIABLE or _first_claim(claims, ClaimPredicate.OPT_OUT):
        return CompletenessState.NOT_APPLICABLE, (), []
    if (
        approval is ApprovalClass.HUMAN_REQUIRED
        or entity.entity_type is EntityType.CONTACT
        or fit is FitState.REVIEW and entity.relationship == "alternate"
    ):
        return CompletenessState.BLOCKED, (), []

    required = tuple(sorted(set(request.contract.required_fields)))
    if not required:
        return CompletenessState.INCOMPLETE, (), []
    present = {predicate.value for predicate in claims}
    missing = tuple(field for field in required if field not in present)
    if missing:
        return CompletenessState.INCOMPLETE, missing, ["required_facts_missing"]
    return CompletenessState.COMPLETE, (), ["required_facts_complete"]


def _conversation_state(
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    fit: FitState,
    completeness: CompletenessState,
    approval: ApprovalClass,
) -> ConversationState:
    if _first_claim(claims, ClaimPredicate.OPT_OUT):
        return ConversationState.TERMINAL_INTENT
    if fit is FitState.NONVIABLE:
        return ConversationState.TERMINAL_INTENT
    if completeness is CompletenessState.COMPLETE:
        return ConversationState.TERMINAL_INTENT
    if _first_claim(claims, ClaimPredicate.RETURN_DATE):
        return ConversationState.WAITING_BROKER
    if approval is ApprovalClass.HUMAN_REQUIRED:
        return ConversationState.REVIEW
    return ConversationState.ACTIVE


def _terminal_reason(reason_codes: tuple[str, ...]) -> str:
    for reason in _TERMINAL_REASONS:
        if reason in reason_codes:
            return reason
    raise ValueError("terminal decision has no terminal reason")


def _make_action(
    request: PolicyEvaluationRequest,
    decision: DecisionSnapshot,
    *,
    action_type: ActionType,
    approval_class: ApprovalClass,
    entity: EntityRef,
    source_claims: tuple[Claim, ...],
    sequence: int,
    payload: Mapping[str, Any],
    expected_prior_state: Mapping[str, Any],
    recipient: str = "",
    reason: str,
) -> PlannedAction:
    return PlannedAction.create(
        tenant_id=request.contract.tenant_id,
        client_id=request.contract.client_id,
        campaign_id=request.contract.campaign_id,
        thread_id=request.scope.thread_id,
        sheet_id=request.scope.sheet_id,
        row_anchor=request.scope.row_anchor,
        decision_id=decision.decision_id,
        contract_id=request.contract.contract_id,
        action_type=action_type,
        approval_class=approval_class,
        target_entity_id=entity.entity_id,
        contract_version=request.contract.version,
        snapshot_hash=request.snapshot_hash,
        source_claim_ids=tuple(claim.claim_id for claim in source_claims),
        operation_key=(
            f"{request.snapshot_hash}:{entity.entity_id}:{action_type.value}:{sequence}"
        ),
        expected_prior_state=expected_prior_state,
        dependencies=(),
        sequence=sequence,
        recipient=recipient,
        payload=payload,
        reason=reason,
    )


def _plan_actions(
    request: PolicyEvaluationRequest,
    entity: EntityRef,
    claims: Mapping[ClaimPredicate, tuple[Claim, ...]],
    decision: DecisionSnapshot,
    approval: ApprovalClass,
    source_claims: tuple[Claim, ...],
) -> ActionPlan:
    actions = []
    sequence = 1
    effective_claims = tuple(
        claim
        for predicate in sorted(claims, key=lambda item: item.value)
        for claim in claims[predicate]
    )
    all_claims = source_claims or effective_claims
    current_facts = dict(request.current_facts.get(entity.entity_id, {}))

    if entity.entity_type in _PROPERTY_ENTITY_TYPES and entity.relationship != "alternate":
        for predicate in sorted(_FACT_PREDICATES & set(claims), key=lambda item: item.value):
            source = claims[predicate]
            claim = source[0]
            actions.append(
                _make_action(
                    request,
                    decision,
                    action_type=ActionType.FACT_UPDATE,
                    approval_class=ApprovalClass.AUTOMATIC,
                    entity=entity,
                    source_claims=source,
                    sequence=sequence,
                    payload={
                        "field": predicate.value,
                        "value": claim.value,
                        "confidence": claim.confidence,
                    },
                    expected_prior_state={
                        predicate.value: current_facts.get(predicate.value)
                    },
                    reason=decision.reason_codes[0],
                )
            )
            sequence += 1

    if decision.conversation_state is ConversationState.TERMINAL_INTENT:
        actions.append(
            _make_action(
                request,
                decision,
                action_type=ActionType.FOLLOWUP_FREEZE,
                approval_class=ApprovalClass.AUTOMATIC,
                entity=entity,
                source_claims=all_claims,
                sequence=sequence,
                payload={"reason": _terminal_reason(decision.reason_codes)},
                expected_prior_state={
                    "followUpStatus": request.current_followup_states.get(
                        entity.entity_id, "unknown"
                    )
                },
                reason=_terminal_reason(decision.reason_codes),
            )
        )
        sequence += 1

    current_conversation = str(
        request.current_conversation_states.get(entity.entity_id, "active")
    )
    if current_conversation != decision.conversation_state.value and all_claims:
        actions.append(
            _make_action(
                request,
                decision,
                action_type=ActionType.STATUS_TRANSITION,
                approval_class=ApprovalClass.AUTOMATIC,
                entity=entity,
                source_claims=all_claims,
                sequence=sequence,
                payload={"status": decision.conversation_state.value},
                expected_prior_state={"conversationState": current_conversation},
                reason=decision.reason_codes[0],
            )
        )
        sequence += 1

    identity_claim = _first_claim(claims, ClaimPredicate.IDENTITY)
    if entity.relationship == "alternate" and identity_claim:
        actions.append(
            _make_action(
                request,
                decision,
                action_type=ActionType.ALTERNATE_PROPERTY_PROPOSAL,
                approval_class=ApprovalClass.HUMAN_REQUIRED,
                entity=entity,
                source_claims=(identity_claim,),
                sequence=sequence,
                payload={"summary": str(identity_claim.value)},
                expected_prior_state={},
                reason="alternate_property_requires_approval",
            )
        )
        sequence += 1

    referral = _first_claim(claims, ClaimPredicate.REFERRAL)
    referral_recipient = _referral_recipient(referral)
    if referral and referral_recipient:
        actions.append(
            _make_action(
                request,
                decision,
                action_type=ActionType.RECIPIENT_CHANGE,
                approval_class=ApprovalClass.HUMAN_REQUIRED,
                entity=entity,
                source_claims=(referral,),
                sequence=sequence,
                recipient=referral_recipient,
                payload={"reason": "redirect_requires_approval"},
                expected_prior_state={"recipient": ""},
                reason="redirect_requires_approval",
            )
        )
        sequence += 1

    call = _first_claim(claims, ClaimPredicate.CALL_REQUEST)
    if call:
        actions.append(
            _make_action(
                request,
                decision,
                action_type=ActionType.CALL_REQUEST,
                approval_class=ApprovalClass.HUMAN_REQUIRED,
                entity=entity,
                source_claims=(call,),
                sequence=sequence,
                payload={"notes": "Broker requested a call.", "phone": ""},
                expected_prior_state={},
                reason="call_requires_approval",
            )
        )
        sequence += 1

    if decision.conversation_state is ConversationState.REVIEW and all_claims:
        actions.append(
            _make_action(
                request,
                decision,
                action_type=ActionType.REVIEW_ITEM,
                approval_class=ApprovalClass.HUMAN_REQUIRED,
                entity=entity,
                source_claims=all_claims,
                sequence=sequence,
                payload={
                    "summary": "Campaign decision requires review",
                    "details": {
                        "entityId": entity.entity_id,
                        "reasonCodes": list(decision.reason_codes),
                        "sourceClaimIds": [claim.claim_id for claim in all_claims],
                    },
                },
                expected_prior_state={},
                reason=decision.reason_codes[0],
            )
        )

    plan = ActionPlan.create(
        tenant_id=request.contract.tenant_id,
        client_id=request.contract.client_id,
        campaign_id=request.contract.campaign_id,
        decision_id=decision.decision_id,
        contract_id=request.contract.contract_id,
        contract_version=request.contract.version,
        snapshot_hash=request.snapshot_hash,
        actions=tuple(actions),
    )
    validate_action_plan(
        plan,
        decision,
        scope=request.scope,
        entities=request.entities,
        claims=request.claims,
        authorized_recipients=request.authorized_recipients,
    )
    return plan


def evaluate_policy(request: PolicyEvaluationRequest) -> PolicyEvaluationResult:
    effective, conflicts = _effective_claims(request.claims)
    conflict_entities = {conflict.entity_id for conflict in conflicts}
    superseded_ids = {
        claim.supersedes_claim_id
        for claim in request.claims
        if claim.supersedes_claim_id
    }
    active_source_claims = {
        entity.entity_id: tuple(
            claim
            for claim in request.claims
            if claim.subject_entity_id == entity.entity_id
            and claim.claim_id not in superseded_ids
        )
        for entity in request.entities
    }
    results = []

    for entity in request.entities:
        entity_claims = effective.get(entity.entity_id, {})
        entity_sources = active_source_claims[entity.entity_id]
        market, reasons = _market_state(
            entity_claims,
            has_conflict=entity.entity_id in conflict_entities,
        )
        fit, reasons = _fit_state(request, entity, entity_claims, market, reasons)
        preliminary_approval = _approval_class(entity, entity_claims, reasons)
        completeness, missing, completeness_reasons = _completeness(
            request,
            entity,
            entity_claims,
            fit,
            preliminary_approval,
        )
        reasons.extend(completeness_reasons)
        conversation = _conversation_state(
            entity_claims,
            fit,
            completeness,
            preliminary_approval,
        )
        approval = _approval_class(entity, entity_claims, reasons, conversation)
        if _first_claim(entity_claims, ClaimPredicate.CALL_REQUEST):
            reasons.append("call_requires_approval")
        if _first_claim(entity_claims, ClaimPredicate.OPT_OUT):
            reasons.append("contact_opted_out")
        if _first_claim(entity_claims, ClaimPredicate.RETURN_DATE):
            reasons.append("broker_return_date")

        reason_codes = tuple(sorted(set(reasons)))
        evidence_ids = tuple(
            sorted(
                {
                    claim.evidence_id
                    for claim in entity_sources
                }
            )
        )
        decision = DecisionSnapshot.create(
            tenant_id=request.contract.tenant_id,
            client_id=request.contract.client_id,
            campaign_id=request.contract.campaign_id,
            contract_id=request.contract.contract_id,
            entity_id=entity.entity_id,
            contract_version=request.contract.version,
            snapshot_hash=request.snapshot_hash,
            market_state=market,
            fit_state=fit,
            completeness_state=completeness,
            conversation_state=conversation,
            reason_codes=reason_codes,
            evidence_ids=evidence_ids,
            missing_fields=tuple(sorted(missing)),
        )
        validate_decision(decision, entities=request.entities, contract=request.contract)
        plan = _plan_actions(
            request,
            entity,
            entity_claims,
            decision,
            approval,
            entity_sources,
        )
        source_claim_ids = tuple(
            sorted(claim.claim_id for claim in entity_sources)
        )
        results.append(
            EntityPolicyResult(
                decision=decision,
                approval_class=approval,
                action_plan=plan,
                source_claim_ids=source_claim_ids,
            )
        )

    ordered = tuple(sorted(results, key=lambda item: item.decision.entity_id))
    digest_payload = [item.to_dict() for item in ordered]
    return PolicyEvaluationResult(
        results=ordered,
        conflicts=conflicts,
        result_digest=_canonical_digest(digest_payload),
    )
