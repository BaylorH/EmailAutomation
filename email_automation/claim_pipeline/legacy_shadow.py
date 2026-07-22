"""Pure, no-effect projection and comparison for legacy proposal behavior."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .contracts import (
    ActionType,
    ActorRole,
    ApprovalClass,
    CampaignContract,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    ContractAuthority,
    ConversationState,
    EntityRef,
    EntityType,
    ExecutionScope,
)
from .legacy_shadow_fixtures import LegacyShadowFixtureCase
from .policy import PolicyEvaluationRequest, evaluate_policy
from .policy_fixtures import PolicyFixtureCase


_COLUMN_PREDICATES = {
    "Availability": "availability",
    "Asking Status": "asking_status",
    "Transaction Type": "transaction_type",
    "Total SF": "total_sf",
    "Office SF": "office_sf",
    "Rent/SF/Yr": "rent",
    "Ops Ex / SF": "operating_expenses",
    "Power": "power",
    "Clear Height": "clear_height",
    "Drive Ins": "drive_ins",
    "Docks": "docks",
    "Occupancy Date": "occupancy_date",
    "Term": "term",
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LegacyActionAttempt:
    attempt_id: str
    entity_key: str
    action_type: ActionType
    approval_class: ApprovalClass
    qualifier: str
    source_component: str
    source_index: int

    @classmethod
    def create(
        cls,
        *,
        case_id: str,
        entity_key: str,
        action_type: ActionType,
        approval_class: ApprovalClass,
        qualifier: str,
        source_component: str,
        source_index: int,
        ordinal: int,
    ) -> "LegacyActionAttempt":
        identity = {
            "caseId": case_id,
            "entityKey": entity_key,
            "actionType": action_type.value,
            "approvalClass": approval_class.value,
            "qualifier": qualifier,
            "sourceComponent": source_component,
            "sourceIndex": source_index,
            "ordinal": ordinal,
        }
        return cls(
            attempt_id=f"legacy_attempt_{_digest(identity)[:24]}",
            entity_key=entity_key,
            action_type=action_type,
            approval_class=approval_class,
            qualifier=qualifier,
            source_component=source_component,
            source_index=source_index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "attemptId": self.attempt_id,
            "entityKey": self.entity_key,
            "actionType": self.action_type.value,
            "approvalClass": self.approval_class.value,
            "qualifier": self.qualifier,
            "sourceComponent": self.source_component,
            "sourceIndex": self.source_index,
        }


@dataclass(frozen=True)
class LegacyProjection:
    case_id: str
    attempts: tuple[LegacyActionAttempt, ...]
    projection_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "projectionDigest": self.projection_digest,
        }


@dataclass(frozen=True)
class LegacyShadowDiscrepancy:
    code: str
    category: str
    severity: str
    entity_key: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "entityKey": self.entity_key,
        }


@dataclass(frozen=True)
class LegacyShadowCaseResult:
    case_id: str
    policy_case_id: str
    provenance_kind: str
    source_ref: str
    disposition: str
    severity: str
    discrepancies: tuple[LegacyShadowDiscrepancy, ...]
    legacy_projection_digest: str
    policy_result_digest: str
    result_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "caseId": self.case_id,
            "policyCaseId": self.policy_case_id,
            "provenanceKind": self.provenance_kind,
            "sourceRef": self.source_ref,
            "disposition": self.disposition,
            "severity": self.severity,
            "discrepancies": [item.to_dict() for item in self.discrepancies],
            "legacyProjectionDigest": self.legacy_projection_digest,
            "policyResultDigest": self.policy_result_digest,
            "resultDigest": self.result_digest,
        }


def _attempt_specs_for_event(
    event_type: str,
    *,
    recipient_relation: str,
) -> tuple[tuple[ActionType, ApprovalClass, str], ...]:
    automatic = ApprovalClass.AUTOMATIC
    human = ApprovalClass.HUMAN_REQUIRED
    if event_type == "property_unavailable":
        return (
            (ActionType.FOLLOWUP_FREEZE, automatic, "terminal"),
            (ActionType.STATUS_TRANSITION, automatic, "terminal"),
            (ActionType.ROW_MOVE, automatic, "nonviable"),
        )
    if event_type == "close_conversation":
        return (
            (ActionType.FOLLOWUP_FREEZE, automatic, "terminal"),
            (ActionType.STATUS_TRANSITION, automatic, "terminal"),
        )
    if event_type == "contact_optout":
        return (
            (ActionType.FOLLOWUP_FREEZE, automatic, "terminal"),
            (ActionType.STATUS_TRANSITION, automatic, "terminal"),
            (ActionType.ROW_MOVE, automatic, "nonviable"),
            (ActionType.REVIEW_ITEM, human, "contact_optout"),
        )
    if event_type == "new_property":
        specs = [
            (ActionType.ALTERNATE_PROPERTY_PROPOSAL, human, "approval"),
            (ActionType.REVIEW_ITEM, human, "new_property"),
        ]
        if recipient_relation == "different":
            specs.append((ActionType.RECIPIENT_CHANGE, human, "different"))
        return tuple(specs)
    if event_type == "wrong_contact":
        specs = [
            (ActionType.REVIEW_ITEM, human, "wrong_contact"),
            (ActionType.STATUS_TRANSITION, automatic, "review"),
        ]
        if recipient_relation == "different":
            specs.append((ActionType.RECIPIENT_CHANGE, human, "different"))
        return tuple(specs)
    if event_type == "needs_user_input":
        return (
            (ActionType.REVIEW_ITEM, human, "needs_user_input"),
            (ActionType.STATUS_TRANSITION, automatic, "review"),
        )
    if event_type == "call_requested":
        return (
            (ActionType.CALL_REQUEST, human, "call_requested"),
            (ActionType.REVIEW_ITEM, human, "call_requested"),
            (ActionType.STATUS_TRANSITION, automatic, "review"),
        )
    if event_type == "tour_requested":
        return (
            (ActionType.TOUR_REQUEST, human, "tour_requested"),
            (ActionType.REVIEW_ITEM, human, "tour_requested"),
            (ActionType.STATUS_TRANSITION, automatic, "review"),
        )
    if event_type == "property_issue":
        return (
            (ActionType.NOTE_APPEND, automatic, "property_issue"),
            (ActionType.REVIEW_ITEM, human, "property_issue"),
        )
    raise ValueError(f"unsupported legacy event type {event_type!r}")


def project_legacy_proposal(case: LegacyShadowFixtureCase) -> LegacyProjection:
    attempts: list[LegacyActionAttempt] = []

    def append_attempts(
        specs: Iterable[tuple[ActionType, ApprovalClass, str]],
        *,
        entity_key: str,
        source_component: str,
        source_index: int,
    ) -> None:
        for ordinal, (action_type, approval_class, qualifier) in enumerate(specs):
            attempts.append(
                LegacyActionAttempt.create(
                    case_id=case.case_id,
                    entity_key=entity_key,
                    action_type=action_type,
                    approval_class=approval_class,
                    qualifier=qualifier,
                    source_component=source_component,
                    source_index=source_index,
                    ordinal=ordinal,
                )
            )

    for index, update in enumerate(case.legacy_proposal.updates):
        append_attempts(
            (
                (
                    ActionType.FACT_UPDATE,
                    ApprovalClass.AUTOMATIC,
                    _COLUMN_PREDICATES[str(update["column"])],
                ),
            ),
            entity_key=case.bindings.current_entity,
            source_component="update",
            source_index=index,
        )

    for index, (event, entity_key) in enumerate(
        zip(case.legacy_proposal.events, case.bindings.event_entities)
    ):
        append_attempts(
            _attempt_specs_for_event(
                str(event["type"]),
                recipient_relation=case.bindings.recipient_relation,
            ),
            entity_key=entity_key,
            source_component="event",
            source_index=index,
        )

    if case.legacy_proposal.response_draft and not case.legacy_proposal.skip_response:
        append_attempts(
            ((ActionType.OUTBOUND_DRAFT, ApprovalClass.AUTOMATIC, "draft"),),
            entity_key=case.bindings.current_entity,
            source_component="response",
            source_index=0,
        )

    ordered = tuple(sorted(attempts, key=lambda attempt: attempt.attempt_id))
    payload = [attempt.to_dict() for attempt in ordered]
    return LegacyProjection(
        case_id=case.case_id,
        attempts=ordered,
        projection_digest=_digest(payload),
    )


def _build_policy_request(
    case: PolicyFixtureCase,
) -> tuple[PolicyEvaluationRequest, Mapping[str, EntityRef]]:
    contract = CampaignContract.create(
        tenant_id="tenant-1",
        client_id="client-1",
        campaign_id="campaign-1",
        version=int(case.contract.get("version", 1)),
        required_fields=tuple(case.contract.get("requiredFields", ())),
        hard_requirements=dict(case.contract.get("hardRequirements", {})),
        soft_preferences=dict(case.contract.get("softPreferences", {})),
        source_authority=ContractAuthority.SYSTEM_POLICY,
    )
    entities_by_key = {
        str(item["key"]): EntityRef.create(
            tenant_id="tenant-1",
            campaign_id="campaign-1",
            entity_type=EntityType(item["type"]),
            label=str(item["key"]),
            canonical_address=(
                "100 Target Rd"
                if item["relationship"] != "alternate"
                else "900 Replacement Rd"
            ),
            suite=str(item["key"]) if item["type"] == "suite" else "",
            relationship=str(item["relationship"]),
        )
        for item in case.entities
    }
    claims_by_key: dict[str, Claim] = {}
    for index, item in enumerate(case.claims):
        supersedes = item.get("supersedes")
        claims_by_key[str(item["key"])] = Claim.create(
            tenant_id="tenant-1",
            evidence_id=f"evidence-{case.case_id}-{index}",
            subject_entity_id=entities_by_key[str(item["subject"])].entity_id,
            predicate=ClaimPredicate(item["predicate"]),
            value=item["value"],
            evidence_text=f"fixture evidence {item['key']}",
            actor_role=ActorRole.BROKER,
            polarity=ClaimPolarity(item["polarity"]),
            modality=ClaimModality(item["modality"]),
            confidence=0.99,
            supersedes_claim_id=(
                claims_by_key[str(supersedes)].claim_id if supersedes else None
            ),
            campaign_id="campaign-1",
            actor_email="broker@example.test",
            observed_at=f"2026-07-22T12:{index:02d}:00Z",
        )

    def remap(values: Mapping[str, Any]) -> dict[str, Any]:
        return {
            entities_by_key[str(key)].entity_id: dict(value)
            for key, value in values.items()
        }

    request = PolicyEvaluationRequest.create(
        contract=contract,
        scope=ExecutionScope(
            tenant_id="tenant-1",
            client_id="client-1",
            campaign_id="campaign-1",
            thread_id="thread-1",
            sheet_id="sheet-1",
            row_anchor="100 Target Rd",
        ),
        entities=tuple(entities_by_key.values()),
        claims=tuple(claims_by_key.values()),
        snapshot_hash=f"snapshot-{case.case_id}",
        current_facts=remap(case.current_state["facts"]),
        current_conversation_states={
            entities_by_key[str(key)].entity_id: value
            for key, value in case.current_state["conversationStates"].items()
        },
        current_followup_states={
            entities_by_key[str(key)].entity_id: value
            for key, value in case.current_state["followupStates"].items()
        },
        authorized_recipients=("broker@example.test",),
    )
    return request, entities_by_key


def _policy_action_sets(
    policy_result: Any,
    entities_by_key: Mapping[str, EntityRef],
) -> tuple[
    set[tuple[str, str]],
    set[str],
    set[str],
    set[str],
    set[str],
    set[str],
    set[str],
    set[str],
    Mapping[str, Any],
]:
    key_by_id = {entity.entity_id: key for key, entity in entities_by_key.items()}
    facts: set[tuple[str, str]] = set()
    freezes: set[str] = set()
    terminal_statuses: set[str] = set()
    waiting_statuses: set[str] = set()
    reviews: set[str] = set()
    recipients: set[str] = set()
    alternates: set[str] = set()
    calls: set[str] = set()
    decisions = {}
    for item in policy_result.results:
        entity_key = key_by_id[item.decision.entity_id]
        decisions[entity_key] = item
        for action in item.action_plan.actions:
            if action.action_type is ActionType.FACT_UPDATE:
                facts.add((entity_key, str(action.payload["field"])))
            elif action.action_type is ActionType.FOLLOWUP_FREEZE:
                freezes.add(entity_key)
            elif action.action_type is ActionType.STATUS_TRANSITION:
                status = str(action.payload["status"])
                if status == ConversationState.TERMINAL_INTENT.value:
                    terminal_statuses.add(entity_key)
                elif status == ConversationState.WAITING_BROKER.value:
                    waiting_statuses.add(entity_key)
            elif action.action_type is ActionType.REVIEW_ITEM:
                reviews.add(entity_key)
            elif action.action_type is ActionType.RECIPIENT_CHANGE:
                recipients.add(entity_key)
            elif action.action_type is ActionType.ALTERNATE_PROPERTY_PROPOSAL:
                alternates.add(entity_key)
            elif action.action_type is ActionType.CALL_REQUEST:
                calls.add(entity_key)
    return (
        facts,
        freezes,
        terminal_statuses,
        waiting_statuses,
        reviews,
        recipients,
        alternates,
        calls,
        decisions,
    )


_DISCREPANCY_META = {
    "legacy_automatic_outbound_during_review": (
        "legacy_safety_risk",
        "release_blocker",
    ),
    "legacy_bypasses_recipient_approval": (
        "legacy_safety_risk",
        "release_blocker",
    ),
    "legacy_bypasses_required_review": (
        "legacy_safety_risk",
        "release_blocker",
    ),
    "legacy_market_fit_conflation": (
        "legacy_safety_risk",
        "release_blocker",
    ),
    "legacy_missing_alternate_property_review": (
        "new_policy_gap",
        "release_blocker",
    ),
    "legacy_missing_call_request": ("new_policy_gap", "release_blocker"),
    "legacy_missing_policy_fact": ("new_policy_gap", "release_blocker"),
    "legacy_missing_terminal_freeze": ("new_policy_gap", "release_blocker"),
    "legacy_missing_terminal_status": ("new_policy_gap", "release_blocker"),
    "legacy_outbound_after_optout": ("legacy_safety_risk", "release_blocker"),
    "legacy_terminalizes_nonterminal": (
        "legacy_safety_risk",
        "release_blocker",
    ),
    "legacy_unapproved_recipient_change": (
        "legacy_safety_risk",
        "release_blocker",
    ),
    "legacy_unplanned_fact_mutation": (
        "legacy_safety_risk",
        "release_blocker",
    ),
    "outbound_surface_deferred": ("deferred_surface", "warning"),
    "policy_adds_waiting_state": ("expected_improvement", "info"),
    "row_move_surface_deferred": ("deferred_surface", "warning"),
    "unclassified_difference": ("legacy_safety_risk", "release_blocker"),
}
_DISPOSITION_PRIORITY = {
    "equivalent": 0,
    "expected_improvement": 1,
    "deferred_surface": 2,
    "new_policy_gap": 3,
    "legacy_safety_risk": 4,
}
_SEVERITY_PRIORITY = {"none": 0, "info": 1, "warning": 2, "release_blocker": 3}


def compare_legacy_case(
    case: LegacyShadowFixtureCase,
    policy_case: PolicyFixtureCase,
) -> LegacyShadowCaseResult:
    if case.policy_case_id != policy_case.case_id:
        raise ValueError("shadow case does not reference supplied policy case")
    projection = project_legacy_proposal(case)
    request, entities_by_key = _build_policy_request(policy_case)
    policy_result = evaluate_policy(request)
    (
        policy_facts,
        policy_freezes,
        policy_terminal_statuses,
        policy_waiting_statuses,
        policy_reviews,
        policy_recipients,
        policy_alternates,
        policy_calls,
        policy_decisions,
    ) = _policy_action_sets(policy_result, entities_by_key)

    legacy_facts = {
        (item.entity_key, item.qualifier)
        for item in projection.attempts
        if item.action_type is ActionType.FACT_UPDATE
    }
    legacy_freezes = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.FOLLOWUP_FREEZE
    }
    legacy_terminal_statuses = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.STATUS_TRANSITION
        and item.qualifier == "terminal"
    }
    legacy_reviews = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.REVIEW_ITEM
    }
    legacy_recipients = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.RECIPIENT_CHANGE
    }
    legacy_alternates = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.ALTERNATE_PROPERTY_PROPOSAL
    }
    legacy_calls = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.CALL_REQUEST
    }
    legacy_row_moves = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.ROW_MOVE
    }
    outbound_entities = {
        item.entity_key
        for item in projection.attempts
        if item.action_type is ActionType.OUTBOUND_DRAFT
    }

    findings: dict[str, str] = {}

    def add(code: str, entity_key: str) -> None:
        findings.setdefault(code, entity_key)

    for entity_key, _field in sorted(legacy_facts - policy_facts):
        add("legacy_unplanned_fact_mutation", entity_key)
    for entity_key, _field in sorted(policy_facts - legacy_facts):
        add("legacy_missing_policy_fact", entity_key)

    terminal_conflicts = legacy_terminal_statuses - policy_terminal_statuses
    for entity_key in sorted(terminal_conflicts):
        add("legacy_terminalizes_nonterminal", entity_key)
    for entity_key in sorted(policy_freezes - legacy_freezes):
        add("legacy_missing_terminal_freeze", entity_key)
    for entity_key in sorted(policy_terminal_statuses - legacy_terminal_statuses):
        add("legacy_missing_terminal_status", entity_key)

    for entity_key in sorted(policy_reviews - legacy_reviews):
        add("legacy_bypasses_required_review", entity_key)
    for entity_key in sorted(policy_recipients - legacy_recipients):
        add("legacy_bypasses_recipient_approval", entity_key)
    for entity_key in sorted(legacy_recipients - policy_recipients):
        add("legacy_unapproved_recipient_change", entity_key)
    for entity_key in sorted(policy_alternates - legacy_alternates):
        add("legacy_missing_alternate_property_review", entity_key)
    for entity_key in sorted(policy_calls - legacy_calls):
        add("legacy_missing_call_request", entity_key)
    for entity_key in sorted(policy_waiting_statuses):
        add("policy_adds_waiting_state", entity_key)

    for event, entity_key in zip(
        case.legacy_proposal.events, case.bindings.event_entities
    ):
        decision_item = policy_decisions[entity_key]
        if (
            event["type"] == "property_unavailable"
            and event["reason"] != "requirements_mismatch"
            and decision_item.decision.market_state.value == "available"
            and decision_item.decision.fit_state.value == "nonviable"
        ):
            add("legacy_market_fit_conflation", entity_key)

    for entity_key in sorted(legacy_row_moves - terminal_conflicts):
        add("row_move_surface_deferred", entity_key)

    for entity_key in sorted(outbound_entities):
        decision_item = policy_decisions[entity_key]
        if "contact_opted_out" in decision_item.decision.reason_codes:
            add("legacy_outbound_after_optout", entity_key)
        elif (
            decision_item.approval_class is ApprovalClass.HUMAN_REQUIRED
            or entity_key in policy_reviews
        ):
            add("legacy_automatic_outbound_during_review", entity_key)
        else:
            add("outbound_surface_deferred", entity_key)

    discrepancies = tuple(
        LegacyShadowDiscrepancy(
            code=code,
            category=_DISCREPANCY_META[code][0],
            severity=_DISCREPANCY_META[code][1],
            entity_key=findings[code],
        )
        for code in sorted(findings)
    )
    if discrepancies:
        disposition = max(
            (item.category for item in discrepancies),
            key=lambda value: _DISPOSITION_PRIORITY[value],
        )
        severity = max(
            (item.severity for item in discrepancies),
            key=lambda value: _SEVERITY_PRIORITY[value],
        )
    else:
        disposition = "equivalent"
        severity = "none"

    identity = {
        "caseId": case.case_id,
        "policyCaseId": case.policy_case_id,
        "provenanceKind": case.provenance.kind,
        "sourceRef": case.provenance.source_ref,
        "disposition": disposition,
        "severity": severity,
        "discrepancies": [item.to_dict() for item in discrepancies],
        "legacyProjectionDigest": projection.projection_digest,
        "policyResultDigest": policy_result.result_digest,
    }
    return LegacyShadowCaseResult(
        case_id=case.case_id,
        policy_case_id=case.policy_case_id,
        provenance_kind=case.provenance.kind,
        source_ref=case.provenance.source_ref,
        disposition=disposition,
        severity=severity,
        discrepancies=discrepancies,
        legacy_projection_digest=projection.projection_digest,
        policy_result_digest=policy_result.result_digest,
        result_digest=_digest(identity),
    )
