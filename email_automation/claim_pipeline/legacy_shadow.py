"""Pure, no-effect projection and comparison for legacy proposal behavior."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import ActionType, ApprovalClass
from .legacy_shadow_fixtures import LegacyShadowFixtureCase


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
