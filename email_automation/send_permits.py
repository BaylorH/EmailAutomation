"""Durable, retained one-use permits for Microsoft Graph reply sends.

The active thread pointer is only an index. Every permit is retained beneath
the thread so an expired worker, takeover, or later generation cannot erase an
ambiguous provider attempt.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import binascii
import hashlib
import json
import re
from typing import Any, Dict, Optional
from uuid import uuid4

from google.cloud.firestore import SERVER_TIMESTAMP

from .sent_mail_guard import (
    GRAPH_EXACT_SENT_ATTACHMENT_LIMIT,
    canonical_graph_body_hash,
)


GRAPH_SEND_PERMIT_VERSION = 1
GRAPH_SEND_PERMIT_LEASE_SECONDS = 300
GRAPH_SEND_PROVIDER_DEADLINE_SECONDS = 45
GRAPH_SEND_HTTP_MAX_SECONDS = 30
GRAPH_SEND_HTTP_MIN_SECONDS = 1
GRAPH_DRAFT_PREPARATION_VERSION = 1
PENDING_GRAPH_SENT_RECHECK_LIMIT = 8
GRAPH_DRAFT_ATTACHMENT_LIMIT = GRAPH_EXACT_SENT_ATTACHMENT_LIMIT
PENDING_COMPLETION_OBLIGATION_COLLECTION = (
    "pendingResponseCompletionObligations"
)
PENDING_COMPLETION_OBLIGATION_VERSION = 1
PENDING_COMPLETION_OBLIGATION_KIND = (
    "pending_response_client_completion"
)
GRAPH_SEND_POST_PREPARATION_HISTORY_RESERVE = (
    PENDING_GRAPH_SENT_RECHECK_LIMIT + 8
)

GRAPH_SEND_UNRESOLVED_STATUSES = {
    "issued",
    "request_started",
    "needs_reconciliation",
    "accepted",
    "definitely_not_sent",
    "reconciled_sent",
}
GRAPH_SEND_RESOLVED_STATUSES = {
    "settled_sent",
    "settled_definitely_not_sent",
    "settled_draft_needs_review",
    "settled_draft_review_resolved",
    "settled_ambiguous_no_retry",
}
GRAPH_SEND_STATUSES = (
    GRAPH_SEND_UNRESOLVED_STATUSES | GRAPH_SEND_RESOLVED_STATUSES
)
RESOLVED_TERMINAL_GRAPH_REVIEW_STATUSES = {
    "reconciled_sent",
}
RESOLVED_PENDING_DRAFT_REVIEW_STATUSES = {
    "resolved_not_actionable",
}


def graph_send_permit_blocks_new_send(
    permit: Optional[Dict[str, Any]],
) -> bool:
    """Return whether retained permit state forbids another send capability.

    ``settled_draft_needs_review`` is issuer-settled (so the expired pending
    document can be retired), but it is intentionally *not* operator-resolved.
    A retained provider draft remains actionable until an authenticated local
    operator records the exact no-longer-actionable resolution.
    """
    if not permit:
        return False
    return bool(
        permit.get("status") not in GRAPH_SEND_RESOLVED_STATUSES
        or permit.get("status") == "settled_draft_needs_review"
        or permit.get("draftReviewRequired") is True
    )

_PERMIT_IMMUTABLE_FIELDS = (
    "version",
    "permitId",
    "issuerKind",
    "issuerOwner",
    "issuerFence",
    "issuerDocumentId",
    "issuerDocumentPath",
    "threadId",
    "clientId",
    "sourceGraphMessageId",
    "conversationId",
    "recipient",
    "bodyHash",
    "envelopeHash",
    "capabilityHash",
    "providerOperation",
    "issuedAt",
    "leaseUntil",
    "providerDeadline",
)

_PERMIT_STATE_FIELDS = {
    "immutableHash",
    "status",
    "updatedAt",
    "draftPreparation",
    "preparedEnvelope",
    "requestStartedAt",
    "sendPreparedEnvelopeHash",
    "capabilityConsumedAt",
    "providerTimeoutSeconds",
    "resolvedAt",
    "resolutionEvidence",
    "resolutionEvidenceHash",
    "issuerSettledAt",
    "terminalSentEvidence",
    "draftReviewRequired",
    "draftReviewEvidenceRef",
    "draftReviewEvidenceHash",
    "reconciliationRecordedAt",
    "terminalSendReviewRequired",
    "terminalSendReviewEvidenceRef",
    "terminalSendReviewEvidenceHash",
    "terminalResolvedReviewEvidenceHash",
    "pendingSendReviewRequired",
    "pendingReconciliationEvidenceHash",
    "pendingReconciliationRecordedAt",
    "operatorSettlementAuditRef",
    "operatorSettlementAuditHash",
    "operatorOriginalReconciliationEvidenceHash",
    "operatorResolvedReviewEvidenceHash",
    "operatorResolution",
    "stateRevision",
    "stateHeadHash",
    "stateHistory",
}
_PERMIT_ALLOWED_FIELDS = set(_PERMIT_IMMUTABLE_FIELDS) | _PERMIT_STATE_FIELDS
_PERMIT_STATE_HISTORY_VERSION = 1
_PERMIT_STATE_HISTORY_LIMIT = 128
_PERMIT_STATE_PROJECTION_FIELDS = {
    "status",
    "draftState",
    "preparedEnvelopeHash",
    "requestStartedAt",
    "resolutionEvidenceHash",
    "issuerSettledAt",
    "draftReviewRequired",
    "draftReviewEvidencePath",
    "draftReviewEvidenceHash",
    "pendingReconciliationEvidenceHash",
    "terminalSendReviewEvidenceHash",
    "pendingSendReviewRequired",
    "terminalSendReviewRequired",
}

_DRAFT_PREPARATION_BASE_FIELDS = {
    "version",
    "state",
    "sourceGraphMessageId",
    "createRequestStartedAt",
    "createRequestHash",
    "plannedAttachmentCount",
}
_DRAFT_PREPARATION_CREATE_OUTCOME_FIELDS = {
    "draftId",
    "createOutcome",
    "createOutcomeAt",
    "createOutcomeEvidence",
    "createOutcomeEvidenceHash",
}
_DRAFT_PREPARATION_PATCH_REQUEST_FIELDS = {"patchRequestStartedAt"}
_DRAFT_PREPARATION_PATCH_OUTCOME_FIELDS = {
    "patchOutcome",
    "patchOutcomeAt",
    "patchOutcomeEvidence",
    "patchOutcomeEvidenceHash",
}
_RETAINABLE_PRE_SEND_DRAFT_STATES = {
    "draft_created",
    "patch_applied",
    "attachment_applied",
    "prepared",
}
_ORPHANED_DRAFT_REQUEST_EVENTS = {
    "create_request_started": (
        "draft_create_needs_reconciliation",
        "create_reply",
        "Graph createReplyAll request",
    ),
    "patch_request_started": (
        "draft_patch_needs_reconciliation",
        "patch_draft",
        "Graph draft PATCH request",
    ),
    "attachment_request_started": (
        None,
        "attach_draft",
        "Graph draft attachment request",
    ),
}


class GraphSendPermitError(RuntimeError):
    pass


class GraphSendPermitBlocked(GraphSendPermitError):
    pass


class GraphSendPermitLocalRetryable(GraphSendPermitError):
    """An exact local Firestore transition did not commit and may be retried."""


@dataclass(frozen=True)
class GraphSendCapability:
    permit_id: str
    immutable_hash: str
    issuer_kind: str
    issuer_owner: str
    issuer_fence: Optional[int]
    envelope_hash: str
    capability: str = field(repr=False)
    firestore_client: Any = field(repr=False)
    thread_ref: Any = field(repr=False)
    permit_ref: Any = field(repr=False)
    issuer_ref: Any = field(repr=False)


def _same_document_ref(left: Any, right: Any) -> bool:
    """Compare Firestore references by canonical path, never document id alone."""
    if left is right:
        return True
    left_path = getattr(left, "path", None)
    right_path = getattr(right, "path", None)
    return bool(
        isinstance(left_path, str)
        and isinstance(right_path, str)
        and left_path
        and left_path == right_path
    )


def _utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        if hasattr(value, "to_datetime"):
            value = value.to_datetime()
        elif isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
    except Exception:
        return None
    return None


def _hash(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _stable_evidence_hash(payload: Dict[str, Any]) -> str:
    def canonical(value: Any) -> Any:
        path = getattr(value, "path", None)
        if isinstance(path, str) and path:
            return {"__firestoreDocumentPath__": path}
        if isinstance(value, dict):
            return {
                key: canonical(nested)
                for key, nested in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [canonical(nested) for nested in value]
        return value

    return _hash(canonical({
        key: value
        for key, value in dict(payload or {}).items()
        if key
        not in {
            "createdAt",
            "updatedAt",
            "movedAt",
            "deadLetteredAt",
        }
    }))


_PENDING_COMPLETION_IMMUTABLE_FIELDS = frozenset({
    "version",
    "kind",
    "userId",
    "clientId",
    "threadId",
    "pendingDocumentId",
    "sourceGraphMessageId",
    "pendingEnvelopeHash",
    "permitId",
    "permitImmutableHash",
    "sentEvidenceHash",
    "completeClientAfterReply",
})
_PENDING_COMPLETION_DOCUMENT_FIELDS = frozenset({
    "version",
    "obligationId",
    "immutable",
    "immutableHash",
    "status",
    "completionOutcome",
    "settledAt",
    "createdAt",
    "updatedAt",
})
PENDING_COMPLETION_SETTLED_OUTCOMES = frozenset({
    "client_completed",
    "client_ineligible",
    "not_required",
})


def pending_completion_obligation_payload(
    *,
    user_id: str,
    client_id: str,
    thread_id: str,
    pending_document_id: str,
    source_graph_message_id: str,
    pending_envelope_hash_value: str,
    permit_id: str,
    permit_immutable_hash: str,
    sent_evidence: Dict[str, Any],
    complete_client_after_reply: bool,
) -> tuple[str, Dict[str, Any]]:
    """Build one deterministic local-completion tombstone."""
    immutable = {
        "version": PENDING_COMPLETION_OBLIGATION_VERSION,
        "kind": PENDING_COMPLETION_OBLIGATION_KIND,
        "userId": str(user_id or "").strip(),
        "clientId": str(client_id or "").strip(),
        "threadId": str(thread_id or "").strip(),
        "pendingDocumentId": str(pending_document_id or "").strip(),
        "sourceGraphMessageId": str(source_graph_message_id or "").strip(),
        "pendingEnvelopeHash": str(
            pending_envelope_hash_value or ""
        ).strip(),
        "permitId": str(permit_id or "").strip(),
        "permitImmutableHash": str(permit_immutable_hash or "").strip(),
        "sentEvidenceHash": _stable_evidence_hash(sent_evidence),
        "completeClientAfterReply": complete_client_after_reply,
    }
    immutable_hash = _hash(immutable)
    obligation_id = f"pending-completion-{immutable_hash}"
    payload = {
        "version": PENDING_COMPLETION_OBLIGATION_VERSION,
        "obligationId": obligation_id,
        "immutable": immutable,
        "immutableHash": immutable_hash,
        "status": "owed",
        "completionOutcome": None,
        "settledAt": None,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    validate_pending_completion_obligation_payload(
        payload,
        document_id=obligation_id,
        expected_user_id=immutable["userId"],
    )
    return obligation_id, payload


def _pending_completion_timestamp(value: Any) -> bool:
    if value is SERVER_TIMESTAMP:
        return True
    return bool(
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def validate_pending_completion_obligation_payload(
    payload: Any,
    *,
    document_id: Optional[str] = None,
    expected_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate an owed completion obligation or its retained settlement."""
    if (
        not isinstance(payload, dict)
        or set(payload) != _PENDING_COMPLETION_DOCUMENT_FIELDS
        or type(payload.get("version")) is not int
        or payload.get("version") != PENDING_COMPLETION_OBLIGATION_VERSION
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation document schema is malformed"
        )
    immutable = payload.get("immutable")
    if (
        not isinstance(immutable, dict)
        or set(immutable) != _PENDING_COMPLETION_IMMUTABLE_FIELDS
        or type(immutable.get("version")) is not int
        or immutable.get("version")
        != PENDING_COMPLETION_OBLIGATION_VERSION
        or type(immutable.get("kind")) is not str
        or immutable.get("kind") != PENDING_COMPLETION_OBLIGATION_KIND
        or type(immutable.get("completeClientAfterReply")) is not bool
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation immutable schema is malformed"
        )
    required_strings = (
        "userId",
        "threadId",
        "pendingDocumentId",
        "sourceGraphMessageId",
        "pendingEnvelopeHash",
        "permitId",
        "permitImmutableHash",
        "sentEvidenceHash",
    )
    if any(
        type(immutable.get(field_name)) is not str
        or not immutable[field_name].strip()
        or immutable[field_name] != immutable[field_name].strip()
        for field_name in required_strings
    ) or (
        type(immutable.get("clientId")) is not str
        or immutable["clientId"] != immutable["clientId"].strip()
        or immutable["completeClientAfterReply"]
        is not bool(immutable["clientId"])
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation identity binding is malformed"
        )
    if (
        not immutable["permitId"].startswith("graph-send-")
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", immutable[field_name])
            for field_name in (
                "pendingEnvelopeHash",
                "permitImmutableHash",
                "sentEvidenceHash",
            )
        )
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation hash binding is malformed"
        )
    immutable_hash = _hash(immutable)
    obligation_id = f"pending-completion-{immutable_hash}"
    supplied_document_id = (
        str(document_id or "") if document_id is not None else None
    )
    supplied_user_id = (
        str(expected_user_id or "")
        if expected_user_id is not None
        else None
    )
    if (
        type(payload.get("immutableHash")) is not str
        or payload.get("immutableHash") != immutable_hash
        or type(payload.get("obligationId")) is not str
        or payload.get("obligationId") != obligation_id
        or payload["obligationId"] != payload["obligationId"].strip()
        or (
            supplied_document_id is not None
            and (
                supplied_document_id != supplied_document_id.strip()
                or supplied_document_id != obligation_id
            )
        )
        or (
            supplied_user_id is not None
            and (
                supplied_user_id != supplied_user_id.strip()
                or immutable.get("userId") != supplied_user_id
            )
        )
        or not _pending_completion_timestamp(payload.get("createdAt"))
        or not _pending_completion_timestamp(payload.get("updatedAt"))
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation immutable hash or path drifted"
        )
    created_at = payload.get("createdAt")
    updated_at = payload.get("updatedAt")
    if (
        isinstance(created_at, datetime)
        and isinstance(updated_at, datetime)
        and updated_at < created_at
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation timestamp order is malformed"
        )
    status = payload.get("status")
    if type(status) is not str:
        raise GraphSendPermitBlocked(
            "pending completion obligation status is malformed"
        )
    if status == "owed":
        if (
            payload.get("completionOutcome") is not None
            or payload.get("settledAt") is not None
        ):
            raise GraphSendPermitBlocked(
                "owed pending completion obligation contains settlement state"
            )
    elif status == "settled":
        if (
            type(payload.get("completionOutcome")) is not str
            or payload.get("completionOutcome")
            not in PENDING_COMPLETION_SETTLED_OUTCOMES
            or (
                immutable["completeClientAfterReply"]
                and payload.get("completionOutcome") == "not_required"
            )
            or (
                immutable["completeClientAfterReply"] is False
                and payload.get("completionOutcome") != "not_required"
            )
            or not _pending_completion_timestamp(payload.get("settledAt"))
            or payload.get("updatedAt") != payload.get("settledAt")
            or (
                isinstance(payload.get("createdAt"), datetime)
                and isinstance(payload.get("settledAt"), datetime)
                and payload["settledAt"] < payload["createdAt"]
            )
        ):
            raise GraphSendPermitBlocked(
                "settled pending completion tombstone is malformed"
            )
    else:
        raise GraphSendPermitBlocked(
            "pending completion obligation status is malformed"
        )
    return dict(payload)


def _state_time(value: Any) -> Optional[str]:
    parsed = _utc(value)
    return parsed.isoformat() if parsed is not None else None


def _permit_state_projection(raw: Dict[str, Any]) -> Dict[str, Any]:
    preparation = raw.get("draftPreparation")
    preparation = preparation if isinstance(preparation, dict) else {}
    envelope = raw.get("preparedEnvelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    draft_review_ref = raw.get("draftReviewEvidenceRef")
    draft_review_path = getattr(draft_review_ref, "path", None)
    draft_review_path = (
        draft_review_path.strip("/")
        if isinstance(draft_review_path, str)
        and draft_review_path.strip("/")
        else None
    )
    return {
        "status": raw.get("status"),
        "draftState": preparation.get("state"),
        "preparedEnvelopeHash": envelope.get("preparedEnvelopeHash"),
        "requestStartedAt": _state_time(raw.get("requestStartedAt")),
        "resolutionEvidenceHash": raw.get("resolutionEvidenceHash"),
        "issuerSettledAt": _state_time(raw.get("issuerSettledAt")),
        "draftReviewRequired": raw.get("draftReviewRequired"),
        "draftReviewEvidencePath": draft_review_path,
        "draftReviewEvidenceHash": raw.get("draftReviewEvidenceHash"),
        "pendingReconciliationEvidenceHash": raw.get(
            "pendingReconciliationEvidenceHash"
        ),
        "terminalSendReviewEvidenceHash": raw.get(
            "terminalSendReviewEvidenceHash"
        ),
        "pendingSendReviewRequired": raw.get("pendingSendReviewRequired"),
        "terminalSendReviewRequired": raw.get("terminalSendReviewRequired"),
    }


def _state_event(
    *,
    revision: int,
    prior_head_hash: Optional[str],
    event: str,
    state: Dict[str, Any],
    occurred_at: datetime,
) -> Dict[str, Any]:
    core = {
        "version": _PERMIT_STATE_HISTORY_VERSION,
        "revision": revision,
        "priorHeadHash": prior_head_hash,
        "event": str(event or "").strip(),
        "occurredAt": _state_time(occurred_at),
        "state": dict(state),
    }
    return {**core, "headHash": _stable_evidence_hash(core)}


def _initial_state_history(permit: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    event = _state_event(
        revision=0,
        prior_head_hash=None,
        event="issued",
        state=_permit_state_projection(permit),
        occurred_at=now,
    )
    return {
        "stateRevision": 0,
        "stateHeadHash": event["headHash"],
        "stateHistory": [event],
    }


def _stateful_permit_patch(
    permit: Dict[str, Any],
    patch: Dict[str, Any],
    *,
    event: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Link one server transition to the retained prior permit state."""
    now = now or datetime.now(timezone.utc)
    revision = permit.get("stateRevision")
    history = permit.get("stateHistory")
    prior_head = permit.get("stateHeadHash")
    if (
        type(revision) is not int
        or revision < 0
        or not isinstance(history, list)
        or len(history) != revision + 1
        or not str(prior_head or "").strip()
        or len(history) >= _PERMIT_STATE_HISTORY_LIMIT
    ):
        raise GraphSendPermitBlocked(
            "Graph send permit transition history is missing or malformed"
        )
    candidate = {**permit, **dict(patch)}
    next_event = _state_event(
        revision=revision + 1,
        prior_head_hash=prior_head,
        event=event,
        state=_permit_state_projection(candidate),
        occurred_at=now,
    )
    state_patch = {
        **dict(patch),
        "stateRevision": revision + 1,
        "stateHeadHash": next_event["headHash"],
        "stateHistory": [*history, next_event],
    }
    # Validate the exact post-transition document before any transaction write.
    # This keeps a coupled thread/pending CAS from committing an invalid permit
    # that would only be detected by a later reader.
    _validate_permit({**permit, **state_patch})
    return state_patch


def _state_projection_changes(
    prior: Dict[str, Any],
    current: Dict[str, Any],
) -> set[str]:
    return {
        field
        for field in _PERMIT_STATE_PROJECTION_FIELDS
        if prior.get(field) != current.get(field)
    }


def _valid_state_event_transition(
    prior: Dict[str, Any],
    current: Dict[str, Any],
    event: str,
) -> bool:
    """Recognize only server protocol transitions represented by one event."""
    if (
        set(prior) != _PERMIT_STATE_PROJECTION_FIELDS
        or set(current) != _PERMIT_STATE_PROJECTION_FIELDS
    ):
        return False
    changed = _state_projection_changes(prior, current)
    prior_status = prior.get("status")
    status = current.get("status")
    prior_draft = prior.get("draftState")
    draft = current.get("draftState")

    exact_transitions = {
        "draft_create_requested": (
            "issued", None, "issued", "create_request_started", {"draftState"}
        ),
        "draft_create_created": (
            "issued", "create_request_started", "issued", "draft_created", {"draftState"}
        ),
        "draft_create_definitely_not_created": (
            "issued",
            "create_request_started",
            "definitely_not_sent",
            "create_definitely_not_created",
            {"status", "draftState", "resolutionEvidenceHash"},
        ),
        "draft_create_needs_reconciliation": (
            "issued",
            "create_request_started",
            "needs_reconciliation",
            "draft_mutation_needs_reconciliation",
            {"status", "draftState", "resolutionEvidenceHash"},
        ),
        "draft_patch_requested": (
            "issued",
            "draft_created",
            "issued",
            "patch_request_started",
            {"draftState", "preparedEnvelopeHash"},
        ),
        "draft_patch_applied": (
            "issued", "patch_request_started", "issued", "patch_applied", {"draftState"}
        ),
        "draft_patch_needs_reconciliation": (
            "issued",
            "patch_request_started",
            "needs_reconciliation",
            "draft_mutation_needs_reconciliation",
            {"status", "draftState", "resolutionEvidenceHash"},
        ),
        "draft_prepared": (
            "issued", None, "issued", "prepared", {"draftState"}
        ),
        "send_request_started": (
            "issued",
            "prepared",
            "request_started",
            "prepared",
            {"status", "requestStartedAt"},
        ),
        "provider_accepted": (
            "request_started",
            "prepared",
            "accepted",
            "prepared",
            {"status", "resolutionEvidenceHash"},
        ),
        "provider_reconciled_sent": (
            "needs_reconciliation",
            "prepared",
            "reconciled_sent",
            "prepared",
            {"status", "resolutionEvidenceHash"},
        ),
    }
    exact = exact_transitions.get(event)
    if exact is not None:
        expected_prior_status, expected_prior_draft, expected_status, expected_draft, expected_changes = exact
        if event == "draft_prepared":
            expected_prior_drafts = {"patch_applied", "attachment_applied"}
            prior_draft_matches = prior_draft in expected_prior_drafts
        else:
            prior_draft_matches = prior_draft == expected_prior_draft
        return bool(
            prior_status == expected_prior_status
            and prior_draft_matches
            and status == expected_status
            and draft == expected_draft
            and changed == expected_changes
            and (
                "resolutionEvidenceHash" not in expected_changes
                or bool(str(current.get("resolutionEvidenceHash") or "").strip())
            )
            and (
                "preparedEnvelopeHash" not in expected_changes
                or bool(str(current.get("preparedEnvelopeHash") or "").strip())
            )
            and (
                "requestStartedAt" not in expected_changes
                or bool(str(current.get("requestStartedAt") or "").strip())
            )
        )

    if event.startswith("draft_attachment_"):
        suffix = event.removeprefix("draft_attachment_")
        if suffix.endswith("_needs_reconciliation"):
            index = suffix[: -len("_needs_reconciliation")]
            return bool(
                index.isdigit()
                and prior_status == "issued"
                and status == "needs_reconciliation"
                and prior_draft == "attachment_request_started"
                and draft == "draft_mutation_needs_reconciliation"
                and changed
                == {"status", "draftState", "resolutionEvidenceHash"}
                and str(current.get("resolutionEvidenceHash") or "").strip()
            )
        operation, separator, outcome = suffix.rpartition("_")
        if not separator or not operation.isdigit():
            return False
        if outcome == "requested":
            return bool(
                prior_status == status == "issued"
                and prior_draft in {"patch_applied", "attachment_applied"}
                and draft == "attachment_request_started"
                and changed == {"draftState"}
            )
        if outcome == "applied":
            return bool(
                prior_status == status == "issued"
                and prior_draft == "attachment_request_started"
                and draft == "attachment_applied"
                and changed == {"draftState"}
            )
        return False

    if event == "provider_definitely_not_sent":
        return bool(
            prior_status == "issued"
            and status == "definitely_not_sent"
            and prior_draft is None
            and draft == prior_draft
            and changed == {"status", "resolutionEvidenceHash"}
            and str(current.get("resolutionEvidenceHash") or "").strip()
        )
    if event == "provider_needs_reconciliation":
        return bool(
            prior_status in {"issued", "request_started"}
            and status == "needs_reconciliation"
            and draft == prior_draft
            and changed == {"status", "resolutionEvidenceHash"}
            and str(current.get("resolutionEvidenceHash") or "").strip()
        )

    settlement_events = {
        "terminal_settled_sent": (
            {"request_started", "needs_reconciliation", "accepted", "reconciled_sent"},
            "settled_sent",
            {"status", "issuerSettledAt", "terminalSendReviewRequired"},
        ),
        "terminal_settled_definitely_not_sent": (
            {"definitely_not_sent"},
            "settled_definitely_not_sent",
            {"status", "issuerSettledAt"},
        ),
        "pending_settled_sent": (
            {"accepted", "reconciled_sent"},
            "settled_sent",
            {"status", "issuerSettledAt"},
        ),
        "pending_settled_definitely_not_sent": (
            {"definitely_not_sent"},
            "settled_definitely_not_sent",
            {"status", "issuerSettledAt"},
        ),
        "operator_ambiguous_no_retry": (
            {"needs_reconciliation"},
            "settled_ambiguous_no_retry",
            {"status", "issuerSettledAt", "pendingSendReviewRequired"},
        ),
    }
    if event == "terminal_settled_draft_needs_review":
        expected_changes = {
            "status",
            "issuerSettledAt",
            "draftReviewRequired",
            "draftReviewEvidenceHash",
        }
        if current.get("draftReviewEvidencePath") is not None:
            expected_changes.add("draftReviewEvidencePath")
        return bool(
            prior_status == "needs_reconciliation"
            and status == "settled_draft_needs_review"
            and draft == prior_draft
            and changed == expected_changes
            and current.get("draftReviewRequired") is True
            and str(current.get("draftReviewEvidenceHash") or "").strip()
            and (
                current.get("draftReviewEvidencePath") is None
                or str(current.get("draftReviewEvidencePath") or "").strip()
            )
        )
    if event == "operator_draft_review_resolved":
        return bool(
            prior_status == "settled_draft_needs_review"
            and status == "settled_draft_review_resolved"
            and draft == prior_draft
            and changed
            == {
                "status",
                "draftReviewRequired",
                "draftReviewEvidenceHash",
            }
            and prior.get("draftReviewRequired") is True
            and current.get("draftReviewRequired") is False
            and str(current.get("draftReviewEvidenceHash") or "").strip()
            and current.get("draftReviewEvidenceHash")
            != prior.get("draftReviewEvidenceHash")
        )
    settlement = settlement_events.get(event)
    if settlement is not None:
        prior_statuses, target, allowed_changes = settlement
        return bool(
            prior_status in prior_statuses
            and status == target
            and draft == prior_draft
            and {"status", "issuerSettledAt"}.issubset(changed)
            and changed.issubset(allowed_changes)
            and str(current.get("issuerSettledAt") or "").strip()
        )

    if event in {
        "terminal_reconciliation_recorded",
        "pending_reconciliation_recorded",
    }:
        marker_prefix = "terminal" if event.startswith("terminal_") else "pending"
        marker_field = f"{marker_prefix}SendReviewRequired"
        hash_field = (
            "terminalSendReviewEvidenceHash"
            if marker_prefix == "terminal"
            else "pendingReconciliationEvidenceHash"
        )
        allowed_changes = {"status", marker_field, hash_field}
        return bool(
            prior_status in {"request_started", "needs_reconciliation", "accepted"}
            and status == "needs_reconciliation"
            and draft == prior_draft
            and changed.issubset(allowed_changes)
            and hash_field in changed
            and current.get(marker_field) is True
            and str(current.get(hash_field) or "").strip()
        )

    if event.startswith("pending_reconcile_"):
        outcome = event.removeprefix("pending_reconcile_")
        targets = {
            "sent": (
                {"request_started", "needs_reconciliation", "accepted", "reconciled_sent"},
                "settled_sent",
            ),
            "draft_needs_review": (
                {"issued", "needs_reconciliation"},
                "settled_draft_needs_review",
            ),
            "send_needs_review": (
                {"request_started", "needs_reconciliation", "accepted"},
                "needs_reconciliation",
            ),
            "definitely_not_sent": (
                {"definitely_not_sent"},
                "settled_definitely_not_sent",
            ),
            "definitely_not_started": (
                {"issued"},
                "settled_definitely_not_sent",
            ),
        }
        target = targets.get(outcome)
        if target is None:
            return False
        prior_statuses, target_status = target
        if (
            outcome == "definitely_not_started"
            and prior_draft is not None
        ) or (
            outcome == "definitely_not_sent"
            and prior_draft
            not in {None, "create_definitely_not_created"}
        ):
            return False
        if outcome == "draft_needs_review":
            expected_changes = {
                "status",
                "issuerSettledAt",
                "draftReviewRequired",
                "draftReviewEvidenceHash",
                "pendingReconciliationEvidenceHash",
            }
            if current.get("draftReviewEvidencePath") is not None:
                expected_changes.add("draftReviewEvidencePath")
            return bool(
                prior_status == "needs_reconciliation"
                and status == "settled_draft_needs_review"
                and draft == prior_draft
                and changed == expected_changes
                and current.get("draftReviewRequired") is True
                and str(current.get("draftReviewEvidenceHash") or "").strip()
                and current.get("pendingReconciliationEvidenceHash")
                == current.get("draftReviewEvidenceHash")
                and str(current.get("issuerSettledAt") or "").strip()
            )
        if outcome in {
            "definitely_not_started",
            "definitely_not_sent",
        } and changed != {
            "status",
            "issuerSettledAt",
            "pendingReconciliationEvidenceHash",
        }:
            return False
        allowed_changes = {
            "status",
            "issuerSettledAt",
            "pendingReconciliationEvidenceHash",
            "pendingSendReviewRequired",
        }
        if not (
            prior_status in prior_statuses
            and status == target_status
            and draft == prior_draft
            and changed.issubset(allowed_changes)
            and "pendingReconciliationEvidenceHash" in changed
            and str(current.get("pendingReconciliationEvidenceHash") or "").strip()
        ):
            return False
        if outcome == "send_needs_review":
            return current.get("pendingSendReviewRequired") is True
        return bool(
            "issuerSettledAt" in changed
            and str(current.get("issuerSettledAt") or "").strip()
        )
    return False


def _body_hash(body: Any) -> str:
    return hashlib.sha256(str(body or "").encode("utf-8")).hexdigest()


def _recipient_addresses(values, *, field: str = "recipient") -> list[str]:
    """Return a canonical recipient projection or fail closed.

    Recipient multiplicity is part of the frozen Graph envelope.  Silently
    dropping malformed or duplicate entries would let the provider mutation
    differ from the envelope authorized by the retained permit.
    """
    if not isinstance(values, (list, tuple)):
        raise GraphSendPermitBlocked(
            f"Graph send {field} recipient collection is malformed"
        )
    addresses = []
    for value in values:
        if isinstance(value, dict):
            email_address = value.get("emailAddress")
            address = (
                email_address.get("address")
                if isinstance(email_address, dict)
                else None
            )
        elif isinstance(value, str):
            address = value
        else:
            address = None
        normalized = str(address or "").strip().lower()
        if not normalized:
            raise GraphSendPermitBlocked(
                f"Graph send {field} recipient is malformed"
            )
        addresses.append(normalized)
    if len(set(addresses)) != len(addresses):
        raise GraphSendPermitBlocked(
            f"Graph send {field} recipients contain duplicates"
        )
    return sorted(addresses)


def _normalize_attachment_content_id(value: Any) -> Optional[str]:
    normalized = str(value or "").strip().strip("<>")
    return normalized or None


def _attachment_semantic_projection(
    value: Dict[str, Any],
    *,
    require_provider_shape: bool = False,
) -> Dict[str, Any]:
    attachment = dict(value or {})
    attachment_type = str(
        attachment.get("@odata.type")
        or ("#microsoft.graph.fileAttachment" if not require_provider_shape else "")
    ).strip().lstrip("#").lower()
    name = str(attachment.get("name") or "").strip()
    content_type = str(attachment.get("contentType") or "").strip().lower()
    content_bytes = attachment.get("contentBytes")
    is_inline = attachment.get("isInline", False)
    content_id = _normalize_attachment_content_id(attachment.get("contentId"))
    provider_id = str(attachment.get("id") or "").strip()
    if (
        attachment_type != "microsoft.graph.fileattachment"
        or not name
        or not content_type
        or not isinstance(content_bytes, str)
        or type(is_inline) is not bool
        or (is_inline and not content_id)
        or (require_provider_shape and not provider_id)
    ):
        raise GraphSendPermitBlocked(
            "Graph send attachment projection is malformed"
        )
    compact_content = re.sub(r"\s+", "", content_bytes)
    try:
        decoded_content = base64.b64decode(
            compact_content.encode("ascii"),
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise GraphSendPermitBlocked(
            "Graph send attachment contentBytes is malformed"
        ) from exc
    return {
        "attachmentType": attachment_type,
        "name": name,
        "contentType": content_type,
        "contentHash": hashlib.sha256(decoded_content).hexdigest(),
        "contentId": content_id,
        "isInline": is_inline,
    }


def _attachment_projection(
    value: Dict[str, Any],
    index: int,
    *,
    require_provider_shape: bool = False,
) -> Dict[str, Any]:
    immutable = {
        "index": index,
        **_attachment_semantic_projection(
            value,
            require_provider_shape=require_provider_shape,
        ),
    }
    return {**immutable, "attachmentHash": _hash(immutable)}


def _attachment_plan(
    values,
    *,
    require_provider_shape: bool = False,
) -> list[Dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        raise GraphSendPermitBlocked(
            "Graph send attachment collection is malformed"
        )
    return [
        _attachment_projection(
            value,
            index,
            require_provider_shape=require_provider_shape,
        )
        for index, value in enumerate(values or [])
    ]


def _required_graph_send_history_entries(
    planned_attachment_count: int,
) -> int:
    # Genesis + create request/outcome + patch request/outcome + ordered
    # attachment request/outcomes + finalize + /send boundary + every bounded
    # post-provider reconciliation/settlement event.
    return (
        1
        + 2
        + 2
        + (2 * planned_attachment_count)
        + 1
        + 1
        + GRAPH_SEND_POST_PREPARATION_HISTORY_RESERVE
    )


def validate_graph_draft_attachment_plan(values: Any) -> int:
    """Validate and bound the full attachment plan before createReplyAll."""
    if not isinstance(values, (list, tuple)):
        raise GraphSendPermitBlocked(
            "Graph draft attachment plan is malformed"
        )
    count = len(values)
    if (
        count > GRAPH_DRAFT_ATTACHMENT_LIMIT
        or _required_graph_send_history_entries(count)
        > _PERMIT_STATE_HISTORY_LIMIT
    ):
        raise GraphSendPermitBlocked(
            "Graph draft attachment plan exceeds the retained history bound"
        )
    plan = _attachment_plan(values)
    inline_content_ids = [
        item.get("contentId")
        for item in plan
        if item.get("isInline") is True
    ]
    if len(set(inline_content_ids)) != len(inline_content_ids):
        raise GraphSendPermitBlocked(
            "Graph draft attachment plan contains duplicate inline content ids"
        )
    return count


def _canonical_attachment_multiset_from_plan(
    plan: Any,
) -> list[Dict[str, Any]]:
    semantic_fields = {
        "attachmentType",
        "name",
        "contentType",
        "contentHash",
        "contentId",
        "isInline",
    }
    if not isinstance(plan, list):
        raise GraphSendPermitBlocked(
            "Graph send attachment plan is malformed"
        )
    projected = []
    for item in plan:
        if not isinstance(item, dict) or not semantic_fields.issubset(item):
            raise GraphSendPermitBlocked(
                "Graph send attachment plan is malformed"
            )
        projected.append({key: item.get(key) for key in semantic_fields})
    return sorted(
        projected,
        key=lambda item: json.dumps(item, sort_keys=True, default=str),
    )


def _canonical_actual_attachment_multiset(
    values: Any,
) -> list[Dict[str, Any]]:
    if not isinstance(values, list):
        raise GraphSendPermitBlocked(
            "Graph send attachment collection is malformed"
        )
    projected = [
        _attachment_semantic_projection(
            value,
            require_provider_shape=True,
        )
        for value in values
    ]
    return sorted(
        projected,
        key=lambda item: json.dumps(item, sort_keys=True, default=str),
    )


def _validate_successful_draft_operation_evidence(
    operation: str,
    evidence: Any,
    *,
    draft_id: str,
    prepared_envelope_hash: Optional[str] = None,
    attachment_index: Optional[int] = None,
    attachment_hash: Optional[str] = None,
) -> None:
    """Require typed provider facts for every successful draft mutation."""
    payload = evidence if isinstance(evidence, dict) else {}
    http_status = payload.get("httpStatus")
    if operation == "create":
        expected_keys = {"phase", "httpStatus", "draftId"}
        valid = bool(
            set(payload) == expected_keys
            and payload.get("phase") == "create_reply"
            and type(http_status) is int
            and http_status in {200, 201}
            and payload.get("draftId") == draft_id
        )
    elif operation == "patch":
        expected_keys = {
            "phase",
            "httpStatus",
            "draftId",
            "preparedEnvelopeHash",
        }
        valid = bool(
            set(payload) == expected_keys
            and payload.get("phase") == "patch_draft"
            and type(http_status) is int
            and http_status in {200, 202, 204}
            and payload.get("draftId") == draft_id
            and payload.get("preparedEnvelopeHash")
            == prepared_envelope_hash
        )
    elif operation == "attachment":
        expected_keys = {
            "phase",
            "httpStatus",
            "draftId",
            "attachmentIndex",
            "attachmentHash",
            "providerAttachmentId",
        }
        valid = bool(
            set(payload) == expected_keys
            and payload.get("phase") == "attach_draft"
            and type(http_status) is int
            and http_status in {200, 201}
            and payload.get("draftId") == draft_id
            and type(payload.get("attachmentIndex")) is int
            and payload.get("attachmentIndex") == attachment_index
            and payload.get("attachmentHash") == attachment_hash
            and str(payload.get("providerAttachmentId") or "").strip()
        )
    else:
        valid = False
    if not valid:
        raise GraphSendPermitBlocked(
            f"Graph draft {operation} success evidence is malformed or mismatched"
        )


def _prepared_envelope(
    capability: GraphSendCapability,
    *,
    source_graph_message_id: str,
    draft_id: str,
    subject: str,
    html_body: str,
    to_recipients,
    cc_recipients,
    attachments,
) -> Dict[str, Any]:
    if not isinstance(subject, str) or not subject.strip():
        raise GraphSendPermitBlocked(
            "Graph draft prepared envelope requires an exact nonempty subject"
        )
    to_addresses = _recipient_addresses(to_recipients, field="To")
    cc_addresses = _recipient_addresses(cc_recipients, field="Cc")
    if set(to_addresses) & set(cc_addresses):
        raise GraphSendPermitBlocked(
            "Graph send To and Cc recipients overlap"
        )
    immutable = {
        "version": GRAPH_DRAFT_PREPARATION_VERSION,
        "parentPermitId": capability.permit_id,
        "parentPermitImmutableHash": capability.immutable_hash,
        "sourceGraphMessageId": str(source_graph_message_id or "").strip(),
        "draftId": str(draft_id or "").strip(),
        "subject": subject,
        "htmlBodyHash": canonical_graph_body_hash(html_body),
        "toRecipients": to_addresses,
        "ccRecipients": cc_addresses,
        "attachments": _attachment_plan(attachments),
    }
    return {**immutable, "preparedEnvelopeHash": _hash(immutable)}


def pending_envelope(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "threadId": (data or {}).get("threadId"),
        "msgId": (data or {}).get("msgId"),
        "recipient": str((data or {}).get("recipient") or "").strip().lower(),
        "clientId": (data or {}).get("clientId"),
        "conversationId": (data or {}).get("conversationId"),
        "responseBodyHash": _body_hash((data or {}).get("responseBody")),
    }


def pending_envelope_hash(data: Dict[str, Any]) -> str:
    return _hash(pending_envelope(data))


def has_terminal_send_marker(thread_data: Dict[str, Any]) -> bool:
    data = thread_data or {}
    return bool(
        data.get("terminalSagaKey")
        or data.get("terminalReplyOwed")
        or data.get("terminalNotificationOwed")
        or data.get("pendingTerminalReason")
        or isinstance(data.get("terminalSaga"), dict)
        or isinstance(data.get("terminalSagaClaim"), dict)
        or isinstance(data.get("terminalReplyAttempt"), dict)
        or isinstance(data.get("terminalSchemaReview"), dict)
    )


def _validate_permit_state_history(raw: Dict[str, Any]) -> None:
    revision = raw.get("stateRevision")
    head_hash = raw.get("stateHeadHash")
    history = raw.get("stateHistory")
    if (
        type(revision) is not int
        or revision < 0
        or not isinstance(history, list)
        or len(history) != revision + 1
        or not history
        or len(history) > _PERMIT_STATE_HISTORY_LIMIT
    ):
        raise GraphSendPermitError(
            "Graph send permit state transition history is malformed"
        )
    prior_head = None
    prior_time = None
    prior_state = None
    for index, raw_event in enumerate(history):
        if not isinstance(raw_event, dict):
            raise GraphSendPermitError(
                "Graph send permit state transition event is malformed"
            )
        event = dict(raw_event)
        event_hash = event.pop("headHash", None)
        occurred_at = _utc(event.get("occurredAt"))
        if (
            event.get("version") != _PERMIT_STATE_HISTORY_VERSION
            or event.get("revision") != index
            or event.get("priorHeadHash") != prior_head
            or not str(event.get("event") or "").strip()
            or not isinstance(event.get("state"), dict)
            or set(event.get("state") or {})
            != _PERMIT_STATE_PROJECTION_FIELDS
            or occurred_at is None
            or (prior_time is not None and occurred_at < prior_time)
            or event_hash != _stable_evidence_hash(event)
        ):
            raise GraphSendPermitError(
                "Graph send permit state transition chain is malformed"
            )
        if index == 0 and (
            event.get("event") != "issued"
            or event.get("priorHeadHash") is not None
            or event.get("state")
            != {
                "status": "issued",
                "draftState": None,
                "preparedEnvelopeHash": None,
                "requestStartedAt": None,
                "resolutionEvidenceHash": None,
                "issuerSettledAt": None,
                "draftReviewRequired": None,
                "draftReviewEvidencePath": None,
                "draftReviewEvidenceHash": None,
                "pendingReconciliationEvidenceHash": None,
                "terminalSendReviewEvidenceHash": None,
                "pendingSendReviewRequired": None,
                "terminalSendReviewRequired": None,
            }
        ):
            raise GraphSendPermitError(
                "Graph send permit state transition genesis is malformed"
            )
        if index > 0 and not _valid_state_event_transition(
            prior_state or {},
            event.get("state") or {},
            str(event.get("event") or ""),
        ):
            raise GraphSendPermitError(
                "Graph send permit state transition sequence is illegal"
            )
        prior_head = event_hash
        prior_time = occurred_at
        prior_state = dict(event.get("state") or {})
    if (
        head_hash != prior_head
        or history[-1].get("state") != _permit_state_projection(raw)
    ):
        raise GraphSendPermitError(
            "Graph send permit current state drifted from transition history"
        )


def _validate_prepared_envelope_state(
    raw: Dict[str, Any],
    envelope: Dict[str, Any],
) -> None:
    immutable = {
        key: envelope.get(key)
        for key in (
            "version",
            "parentPermitId",
            "parentPermitImmutableHash",
            "sourceGraphMessageId",
            "draftId",
            "subject",
            "htmlBodyHash",
            "toRecipients",
            "ccRecipients",
            "attachments",
        )
    }
    attachments = immutable.get("attachments")
    try:
        to_recipients = _recipient_addresses(
            immutable.get("toRecipients"),
            field="prepared To",
        )
        cc_recipients = _recipient_addresses(
            immutable.get("ccRecipients"),
            field="prepared Cc",
        )
    except GraphSendPermitBlocked as exc:
        raise GraphSendPermitError(
            "Graph send permit prepared recipient envelope is malformed"
        ) from exc
    recipients = set(to_recipients) | set(cc_recipients)
    if (
        set(envelope) != set(immutable) | {"preparedEnvelopeHash"}
        or immutable.get("version") != GRAPH_DRAFT_PREPARATION_VERSION
        or immutable.get("parentPermitId") != raw.get("permitId")
        or immutable.get("parentPermitImmutableHash") != raw.get("immutableHash")
        or immutable.get("sourceGraphMessageId")
        != raw.get("sourceGraphMessageId")
        or not str(immutable.get("draftId") or "").strip()
        or not isinstance(immutable.get("subject"), str)
        or not immutable.get("subject").strip()
        or not str(immutable.get("htmlBodyHash") or "").strip()
        or not isinstance(immutable.get("toRecipients"), list)
        or not isinstance(immutable.get("ccRecipients"), list)
        or immutable.get("toRecipients") != to_recipients
        or immutable.get("ccRecipients") != cc_recipients
        or bool(set(to_recipients) & set(cc_recipients))
        or not isinstance(attachments, list)
        or len(attachments)
        != (raw.get("draftPreparation") or {}).get(
            "plannedAttachmentCount"
        )
        or raw.get("recipient") not in recipients
        or envelope.get("preparedEnvelopeHash") != _hash(immutable)
    ):
        raise GraphSendPermitError(
            "Graph send permit prepared envelope schema or hash is malformed"
        )
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            raise GraphSendPermitError(
                "Graph send permit attachment plan is malformed"
            )
        immutable_attachment = {
            key: attachment.get(key)
            for key in (
                "index",
                "attachmentType",
                "name",
                "contentType",
                "contentHash",
                "contentId",
                "isInline",
            )
        }
        if (
            set(attachment) != set(immutable_attachment) | {"attachmentHash"}
            or attachment.get("index") != index
            or attachment.get("attachmentType")
            != "microsoft.graph.fileattachment"
            or not str(attachment.get("name") or "").strip()
            or not str(attachment.get("contentType") or "").strip()
            or not str(attachment.get("contentHash") or "").strip()
            or type(attachment.get("isInline")) is not bool
            or (
                attachment.get("isInline") is True
                and not str(attachment.get("contentId") or "").strip()
            )
            or attachment.get("attachmentHash") != _hash(immutable_attachment)
        ):
            raise GraphSendPermitError(
                "Graph send permit attachment plan hash is malformed"
            )


def _expected_draft_preparation_fields(
    raw: Dict[str, Any],
    preparation: Dict[str, Any],
) -> set[str]:
    state = preparation.get("state")
    fields = set(_DRAFT_PREPARATION_BASE_FIELDS)
    if state == "create_request_started":
        return fields
    fields |= _DRAFT_PREPARATION_CREATE_OUTCOME_FIELDS
    if state in {"draft_created", "create_definitely_not_created"}:
        return fields
    if state == "draft_mutation_needs_reconciliation" and (
        preparation.get("createOutcome") != "created"
    ):
        return fields
    fields |= _DRAFT_PREPARATION_PATCH_REQUEST_FIELDS
    if state == "patch_request_started":
        return fields
    fields |= _DRAFT_PREPARATION_PATCH_OUTCOME_FIELDS
    if state == "patch_applied":
        return fields
    if state == "draft_mutation_needs_reconciliation" and (
        preparation.get("patchOutcome") != "applied"
    ):
        return fields
    if state == "attachment_request_started":
        fields.add("activeAttachment")
        active = preparation.get("activeAttachment")
        if isinstance(active, dict) and active.get("index") not in {None, 0}:
            fields.add("attachmentOutcomes")
        return fields
    if state == "attachment_applied":
        return fields | {"activeAttachment", "attachmentOutcomes"}
    if state == "prepared":
        fields.add("preparedAt")
        envelope = raw.get("preparedEnvelope")
        attachment_plan = (
            envelope.get("attachments")
            if isinstance(envelope, dict)
            else None
        )
        if attachment_plan:
            fields |= {"activeAttachment", "attachmentOutcomes"}
        return fields
    if state == "draft_mutation_needs_reconciliation":
        fields |= {
            "activeAttachment",
            "attachmentOutcomeEvidence",
            "attachmentOutcomeEvidenceHash",
        }
        active = preparation.get("activeAttachment")
        if isinstance(active, dict) and active.get("index") not in {None, 0}:
            fields.add("attachmentOutcomes")
        return fields
    return fields


def _validate_draft_preparation_state(raw: Dict[str, Any]) -> None:
    preparation = raw.get("draftPreparation")
    envelope = raw.get("preparedEnvelope")
    if preparation is None:
        if envelope is not None:
            raise GraphSendPermitError(
                "Graph send permit envelope has no draft operation chain"
            )
        return
    if not isinstance(preparation, dict):
        raise GraphSendPermitError(
            "Graph send permit draft preparation is malformed"
        )
    state = preparation.get("state")
    valid_states = {
        "create_request_started",
        "draft_created",
        "create_definitely_not_created",
        "patch_request_started",
        "patch_applied",
        "attachment_request_started",
        "attachment_applied",
        "prepared",
        "draft_mutation_needs_reconciliation",
    }
    expected_fields = _expected_draft_preparation_fields(raw, preparation)
    if set(preparation) != expected_fields:
        raise GraphSendPermitError(
            "Graph send permit draft preparation state schema is malformed"
        )
    create_started = _utc(preparation.get("createRequestStartedAt"))
    create_request = {
        "version": GRAPH_DRAFT_PREPARATION_VERSION,
        "state": "create_request_started",
        "sourceGraphMessageId": preparation.get("sourceGraphMessageId"),
        "createRequestStartedAt": preparation.get("createRequestStartedAt"),
        "plannedAttachmentCount": preparation.get(
            "plannedAttachmentCount"
        ),
    }
    planned_attachment_count = preparation.get("plannedAttachmentCount")
    if (
        state not in valid_states
        or preparation.get("version") != GRAPH_DRAFT_PREPARATION_VERSION
        or preparation.get("sourceGraphMessageId")
        != raw.get("sourceGraphMessageId")
        or create_started is None
        or type(planned_attachment_count) is not int
        or planned_attachment_count < 0
        or planned_attachment_count > GRAPH_DRAFT_ATTACHMENT_LIMIT
        or _required_graph_send_history_entries(planned_attachment_count)
        > _PERMIT_STATE_HISTORY_LIMIT
        or preparation.get("createRequestHash") != _hash(create_request)
    ):
        raise GraphSendPermitError(
            "Graph send permit draft creation chain is malformed"
        )
    if state == "create_request_started":
        if envelope is not None or preparation.get("draftId") is not None:
            raise GraphSendPermitError(
                "Graph send permit draft-create request contains later state"
            )
        return

    create_outcome = preparation.get("createOutcome")
    create_outcome_at = _utc(preparation.get("createOutcomeAt"))
    create_evidence = preparation.get("createOutcomeEvidence")
    if (
        create_outcome not in {
            "created",
            "definitely_not_created",
            "needs_reconciliation",
        }
        or create_outcome_at is None
        or create_outcome_at < create_started
        or not isinstance(create_evidence, dict)
        or preparation.get("createOutcomeEvidenceHash")
        != _hash(create_evidence)
    ):
        raise GraphSendPermitError(
            "Graph send permit draft-create outcome evidence is malformed"
        )
    if create_outcome != "created":
        if (
            state
            not in {
                "create_definitely_not_created",
                "draft_mutation_needs_reconciliation",
            }
            or preparation.get("draftId") is not None
            or envelope is not None
        ):
            raise GraphSendPermitError(
                "Graph send permit draft-create failure has later operations"
            )
        return
    if not str(preparation.get("draftId") or "").strip():
        raise GraphSendPermitError(
            "Graph send permit created draft id is missing"
        )
    _validate_successful_draft_operation_evidence(
        "create",
        create_evidence,
        draft_id=str(preparation.get("draftId") or "").strip(),
    )
    if state == "draft_created":
        if envelope is not None:
            raise GraphSendPermitError(
                "Graph send permit created draft contains an unstarted patch"
            )
        return
    if not isinstance(envelope, dict):
        raise GraphSendPermitError(
            "Graph send permit draft patch is missing its frozen envelope"
        )
    _validate_prepared_envelope_state(raw, envelope)
    if envelope.get("draftId") != preparation.get("draftId"):
        raise GraphSendPermitError(
            "Graph send permit created draft drifted from prepared envelope"
        )
    patch_started = _utc(preparation.get("patchRequestStartedAt"))
    if patch_started is None or patch_started < create_outcome_at:
        raise GraphSendPermitError(
            "Graph send permit draft patch time regressed"
        )
    if state == "patch_request_started":
        return
    patch_outcome = preparation.get("patchOutcome")
    patch_outcome_at = _utc(preparation.get("patchOutcomeAt"))
    patch_evidence = preparation.get("patchOutcomeEvidence")
    if (
        patch_outcome not in {"applied", "needs_reconciliation"}
        or patch_outcome_at is None
        or patch_outcome_at < patch_started
        or not isinstance(patch_evidence, dict)
        or preparation.get("patchOutcomeEvidenceHash")
        != _hash(patch_evidence)
    ):
        raise GraphSendPermitError(
            "Graph send permit draft patch outcome evidence is malformed"
        )
    if patch_outcome != "applied":
        if state != "draft_mutation_needs_reconciliation":
            raise GraphSendPermitError(
                "Graph send permit failed patch contains later operations"
            )
        return
    _validate_successful_draft_operation_evidence(
        "patch",
        patch_evidence,
        draft_id=str(envelope.get("draftId") or "").strip(),
        prepared_envelope_hash=envelope.get("preparedEnvelopeHash"),
    )

    plan = list(envelope.get("attachments") or [])
    outcomes = preparation.get("attachmentOutcomes") or []
    if not isinstance(outcomes, list) or len(outcomes) > len(plan):
        raise GraphSendPermitError(
            "Graph send permit attachment outcome chain is malformed"
        )
    prior_outcome_at = patch_outcome_at
    for index, outcome in enumerate(outcomes):
        outcome_at = _utc((outcome or {}).get("outcomeAt"))
        evidence = (outcome or {}).get("evidence")
        if (
            not isinstance(outcome, dict)
            or set(outcome)
            != {
                "index",
                "attachmentHash",
                "outcome",
                "outcomeAt",
                "evidence",
                "evidenceHash",
            }
            or outcome.get("index") != index
            or outcome.get("attachmentHash")
            != plan[index].get("attachmentHash")
            or outcome.get("outcome") != "applied"
            or outcome_at is None
            or outcome_at < prior_outcome_at
            or not isinstance(evidence, dict)
            or outcome.get("evidenceHash") != _hash(evidence)
        ):
            raise GraphSendPermitError(
                "Graph send permit attachment outcome chain is malformed"
            )
        _validate_successful_draft_operation_evidence(
            "attachment",
            evidence,
            draft_id=str(envelope.get("draftId") or "").strip(),
            attachment_index=index,
            attachment_hash=plan[index].get("attachmentHash"),
        )
        prior_outcome_at = outcome_at
    active = preparation.get("activeAttachment")
    if state in {
        "attachment_request_started",
        "draft_mutation_needs_reconciliation",
    }:
        if not isinstance(active, dict):
            raise GraphSendPermitError(
                "Graph send permit active attachment request is malformed"
            )
        active_core = {
            "index": active.get("index"),
            "attachmentHash": active.get("attachmentHash"),
            "requestStartedAt": active.get("requestStartedAt"),
        }
        active_time = _utc(active.get("requestStartedAt"))
        if (
            set(active) != {
                "index",
                "attachmentHash",
                "requestStartedAt",
                "requestHash",
            }
            or
            active.get("index") != len(outcomes)
            or active.get("index") >= len(plan)
            or active.get("attachmentHash")
            != plan[active.get("index")].get("attachmentHash")
            or active_time is None
            or active_time < prior_outcome_at
            or active.get("requestHash") != _hash(active_core)
        ):
            raise GraphSendPermitError(
                "Graph send permit active attachment request drifted"
            )
        if state == "draft_mutation_needs_reconciliation":
            attachment_evidence = preparation.get(
                "attachmentOutcomeEvidence"
            )
            if (
                not isinstance(attachment_evidence, dict)
                or preparation.get("attachmentOutcomeEvidenceHash")
                != _hash(attachment_evidence)
            ):
                raise GraphSendPermitError(
                    "Graph send permit attachment reconciliation evidence is malformed"
                )
        return
    if active is not None:
        raise GraphSendPermitError(
            "Graph send permit inactive attachment state retained an active request"
        )
    if state == "attachment_applied" and not outcomes:
        raise GraphSendPermitError(
            "Graph send permit attachment-applied state has no outcome"
        )
    if state == "prepared":
        prepared_at = _utc(preparation.get("preparedAt"))
        if (
            len(outcomes) != len(plan)
            or prepared_at is None
            or prepared_at < prior_outcome_at
        ):
            raise GraphSendPermitError(
                "Graph send permit prepared state skipped ordered operations"
            )
    elif state not in {"patch_applied", "attachment_applied"}:
        if state != "draft_mutation_needs_reconciliation":
            raise GraphSendPermitError(
                "Graph send permit draft state is not a legal operation result"
            )


def _exact_pre_send_draft_resolution(raw: Dict[str, Any]) -> bool:
    preparation = raw.get("draftPreparation") or {}
    evidence = raw.get("resolutionEvidence")
    evidence_hash = raw.get("resolutionEvidenceHash")
    if (
        _utc(raw.get("requestStartedAt")) is not None
        or _utc(raw.get("resolvedAt")) is None
        or not isinstance(evidence, dict)
        or evidence_hash != _hash(evidence)
        or not str(evidence.get("reason") or "").strip()
        or not str(evidence.get("phase") or "").strip()
    ):
        return False

    state = preparation.get("state")
    if state in _RETAINABLE_PRE_SEND_DRAFT_STATES:
        return bool(
            set(evidence)
            == {
                "reason",
                "phase",
                "draftId",
                "providerSendStarted",
                "automaticDeleteAttempted",
            }
            and str(evidence.get("draftId") or "").strip()
            == str(preparation.get("draftId") or "").strip()
            and evidence.get("providerSendStarted") is False
            and evidence.get("automaticDeleteAttempted") is False
        )
    if state != "draft_mutation_needs_reconciliation":
        return False

    if preparation.get("createOutcome") == "needs_reconciliation":
        nested_evidence = preparation.get("createOutcomeEvidence")
        nested_hash = preparation.get("createOutcomeEvidenceHash")
    elif preparation.get("patchOutcome") == "needs_reconciliation":
        nested_evidence = preparation.get("patchOutcomeEvidence")
        nested_hash = preparation.get("patchOutcomeEvidenceHash")
    else:
        nested_evidence = preparation.get("attachmentOutcomeEvidence")
        nested_hash = preparation.get("attachmentOutcomeEvidenceHash")
    return bool(
        isinstance(nested_evidence, dict)
        and evidence == nested_evidence
        and evidence_hash == nested_hash
    )


def _validate_permit_status_state(raw: Dict[str, Any]) -> None:
    status = raw.get("status")
    preparation = raw.get("draftPreparation") or {}
    request_started = _utc(raw.get("requestStartedAt"))
    consumed_at = _utc(raw.get("capabilityConsumedAt"))
    resolved_at = _utc(raw.get("resolvedAt"))
    issuer_settled_at = _utc(raw.get("issuerSettledAt"))
    evidence = raw.get("resolutionEvidence")
    evidence_hash = raw.get("resolutionEvidenceHash")
    retained_draft_review = bool(
        status == "needs_reconciliation"
        and preparation.get("state") in _RETAINABLE_PRE_SEND_DRAFT_STATES
        and _exact_pre_send_draft_resolution(raw)
    )
    has_resolution = any(
        value is not None for value in (raw.get("resolvedAt"), evidence, evidence_hash)
    )
    draft_review_fields = {
        "draftReviewRequired": raw.get("draftReviewRequired"),
        "draftReviewEvidenceRef": raw.get("draftReviewEvidenceRef"),
        "draftReviewEvidenceHash": raw.get("draftReviewEvidenceHash"),
    }
    if status not in {
        "settled_draft_needs_review",
        "settled_draft_review_resolved",
    } and any(
        value is not None for value in draft_review_fields.values()
    ):
        raise GraphSendPermitError(
            "Graph send permit retained draft-review linkage before settlement"
        )
    send_boundary = status in {
        "request_started",
        "accepted",
        "needs_reconciliation",
        "reconciled_sent",
        "settled_sent",
        "settled_ambiguous_no_retry",
    } and request_started is not None
    if request_started is not None:
        envelope = raw.get("preparedEnvelope") or {}
        timeout = raw.get("providerTimeoutSeconds")
        if (
            preparation.get("state") != "prepared"
            or consumed_at is None
            or consumed_at != request_started
            or raw.get("sendPreparedEnvelopeHash")
            != envelope.get("preparedEnvelopeHash")
            or not isinstance(timeout, (int, float))
            or not GRAPH_SEND_HTTP_MIN_SECONDS <= float(timeout) <= GRAPH_SEND_HTTP_MAX_SECONDS
            or request_started < _utc(preparation.get("preparedAt"))
        ):
            raise GraphSendPermitError(
                "Graph send permit request_started state skipped prepared draft evidence"
            )
    if status == "issued":
        if request_started is not None or has_resolution or issuer_settled_at is not None:
            raise GraphSendPermitError(
                "Graph send permit issued state contains later transition evidence"
            )
        return
    if status == "request_started":
        if not send_boundary or has_resolution or issuer_settled_at is not None:
            raise GraphSendPermitError(
                "Graph send permit request_started state is malformed"
            )
        return
    if status in {
        "accepted",
        "needs_reconciliation",
        "reconciled_sent",
        "definitely_not_sent",
    }:
        if (
            resolved_at is None
            or not isinstance(evidence, dict)
            or evidence_hash != _hash(evidence)
            or issuer_settled_at is not None
        ):
            raise GraphSendPermitError(
                "Graph send permit provider resolution evidence is malformed"
            )
        phase = str(evidence.get("phase") or "").strip()
        if status == "accepted" and (
            not send_boundary
            or phase != "send"
            or evidence.get("httpStatus") not in {200, 202}
        ):
            raise GraphSendPermitError(
                "Graph send permit accepted state lacks typed request evidence"
            )
        if status == "definitely_not_sent" and (
            request_started is not None or not phase
        ):
            raise GraphSendPermitError(
                "Graph send permit definitely-unsent state crossed /send or lacks typed evidence"
            )
        if status in {"needs_reconciliation", "reconciled_sent"} and (
            not phase
            or (
                request_started is None
                and preparation.get("state")
                != "draft_mutation_needs_reconciliation"
                and not retained_draft_review
            )
        ):
            raise GraphSendPermitError(
                "Graph send permit reconciliation state lacks typed prior evidence"
            )
        return
    if status not in GRAPH_SEND_RESOLVED_STATUSES:
        raise GraphSendPermitError("Graph send permit status is unknown")
    if issuer_settled_at is None:
        raise GraphSendPermitError(
            "Graph send permit issuer-settled state lacks settlement evidence"
        )
    if status == "settled_sent":
        if (
            not send_boundary
            or not isinstance(raw.get("terminalSentEvidence"), dict)
            or raw.get("pendingSendReviewRequired") is True
            or raw.get("terminalSendReviewRequired") is True
        ):
            raise GraphSendPermitError(
                "Graph send permit settled-sent state lacks exact Sent evidence"
            )
        _validate_exact_terminal_sent_evidence(raw, raw.get("terminalSentEvidence"))
        if (
            raw.get("terminalSendReviewEvidenceHash") is not None
            and raw.get("terminalSendReviewRequired") is False
            and not str(
                raw.get("terminalResolvedReviewEvidenceHash") or ""
            ).strip()
        ):
            raise GraphSendPermitError(
                "Graph send permit settled-sent terminal review linkage is incomplete"
            )
        operator_fields = {
            "operatorSettlementAuditRef": raw.get("operatorSettlementAuditRef"),
            "operatorSettlementAuditHash": raw.get("operatorSettlementAuditHash"),
            "operatorOriginalReconciliationEvidenceHash": raw.get(
                "operatorOriginalReconciliationEvidenceHash"
            ),
            "operatorResolvedReviewEvidenceHash": raw.get(
                "operatorResolvedReviewEvidenceHash"
            ),
            "operatorResolution": raw.get("operatorResolution"),
        }
        if any(value is not None for value in operator_fields.values()) and (
            operator_fields["operatorSettlementAuditRef"] is None
            or not str(
                operator_fields["operatorSettlementAuditHash"] or ""
            ).strip()
            or not str(
                operator_fields[
                    "operatorOriginalReconciliationEvidenceHash"
                ] or ""
            ).strip()
            or not str(
                operator_fields["operatorResolvedReviewEvidenceHash"] or ""
            ).strip()
            or operator_fields["operatorResolution"] != "exact_sent"
            or raw.get("pendingSendReviewRequired") is not False
        ):
            raise GraphSendPermitError(
                "Graph send permit operator exact-Sent settlement is incomplete"
            )
    elif status == "settled_definitely_not_sent":
        if request_started is not None:
            raise GraphSendPermitError(
                "Graph send permit definitely-unsent settlement crossed /send"
            )
    elif status == "settled_draft_needs_review":
        if (
            request_started is not None
            or not _exact_pre_send_draft_resolution(raw)
            or raw.get("draftReviewRequired") is not True
            or raw.get("draftReviewEvidenceRef") is None
            or not str(raw.get("draftReviewEvidenceHash") or "").strip()
            or (
                raw.get("issuerKind") == "pending_response"
                and raw.get("pendingReconciliationEvidenceHash")
                != raw.get("draftReviewEvidenceHash")
            )
        ):
            raise GraphSendPermitError(
                "Graph send permit draft-review settlement is malformed"
            )
    elif status == "settled_draft_review_resolved":
        if (
            request_started is not None
            or not _exact_pre_send_draft_resolution(raw)
            or raw.get("draftReviewRequired") is not False
            or raw.get("draftReviewEvidenceRef") is None
            or not str(raw.get("draftReviewEvidenceHash") or "").strip()
            or raw.get("operatorSettlementAuditRef") is None
            or not str(raw.get("operatorSettlementAuditHash") or "").strip()
            or not str(
                raw.get("operatorOriginalReconciliationEvidenceHash") or ""
            ).strip()
            or not str(
                raw.get("operatorResolvedReviewEvidenceHash") or ""
            ).strip()
            or raw.get("operatorResolvedReviewEvidenceHash")
            != raw.get("draftReviewEvidenceHash")
            or raw.get("operatorResolution")
            != "retained_draft_not_actionable"
            or (
                raw.get("issuerKind") == "pending_response"
                and raw.get("operatorOriginalReconciliationEvidenceHash")
                != raw.get("pendingReconciliationEvidenceHash")
            )
        ):
            raise GraphSendPermitError(
                "Graph send permit resolved draft-review settlement is malformed"
            )
    elif status == "settled_ambiguous_no_retry":
        if (
            not send_boundary
            or raw.get("pendingSendReviewRequired") is not False
            or raw.get("operatorSettlementAuditRef") is None
            or not str(raw.get("operatorSettlementAuditHash") or "").strip()
            or not str(
                raw.get("operatorOriginalReconciliationEvidenceHash") or ""
            ).strip()
            or raw.get("operatorOriginalReconciliationEvidenceHash")
            != raw.get("pendingReconciliationEvidenceHash")
            or not str(
                raw.get("operatorResolvedReviewEvidenceHash") or ""
            ).strip()
            or raw.get("operatorResolution") != "unknown_no_retry"
        ):
            raise GraphSendPermitError(
                "Graph send permit ambiguous-no-retry settlement is malformed"
            )


def _validate_permit_issuer_document(raw: Dict[str, Any]) -> None:
    issuer_kind = raw.get("issuerKind")
    issuer_document_id = str(raw.get("issuerDocumentId") or "").strip()
    issuer_document_path = str(raw.get("issuerDocumentPath") or "").strip("/")
    path_parts = issuer_document_path.split("/") if issuer_document_path else []
    expected_collection = {
        "pending_response": "pendingResponses",
        "terminal_saga": "threads",
    }.get(issuer_kind)
    issuer_fence = raw.get("issuerFence")
    if (
        not issuer_document_id
        or not issuer_document_path
        or "/" in issuer_document_id
        or len(path_parts) < 2
        or path_parts[-1] != issuer_document_id
        or path_parts[-2] != expected_collection
        or (
            issuer_kind == "pending_response"
            and issuer_fence is not None
        )
        or (
            issuer_kind == "terminal_saga"
            and (
                type(issuer_fence) is not int
                or issuer_fence < 1
            )
        )
    ):
        raise GraphSendPermitError(
            "Graph send permit issuer document identity/path is malformed"
        )


def _validate_permit(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise GraphSendPermitError("Graph send permit is missing or malformed")
    immutable = {field: raw.get(field) for field in _PERMIT_IMMUTABLE_FIELDS}
    if (
        raw.get("version") != GRAPH_SEND_PERMIT_VERSION
        or not str(raw.get("permitId") or "").strip()
        or raw.get("issuerKind") not in {"pending_response", "terminal_saga"}
        or not str(raw.get("issuerOwner") or "").strip()
        or not str(raw.get("threadId") or "").strip()
        or not str(raw.get("recipient") or "").strip()
        or not str(raw.get("bodyHash") or "").strip()
        or not str(raw.get("envelopeHash") or "").strip()
        or not str(raw.get("capabilityHash") or "").strip()
        or raw.get("providerOperation") != "graph_reply_send"
        or raw.get("status") not in GRAPH_SEND_STATUSES
        or raw.get("immutableHash") != _hash(immutable)
    ):
        raise GraphSendPermitError(
            "Graph send permit immutable hash or schema is malformed"
        )
    _validate_permit_issuer_document(raw)
    issued_at = _utc(raw.get("issuedAt"))
    lease_until = _utc(raw.get("leaseUntil"))
    provider_deadline = _utc(raw.get("providerDeadline"))
    if (
        issued_at is None
        or lease_until is None
        or provider_deadline is None
        or not issued_at < provider_deadline < lease_until
    ):
        raise GraphSendPermitError("Graph send permit deadlines are malformed")
    unknown_fields = set(raw) - _PERMIT_ALLOWED_FIELDS
    if unknown_fields:
        raise GraphSendPermitError(
            "Graph send permit schema contains unknown state fields: "
            + ", ".join(sorted(unknown_fields))
        )
    _validate_permit_state_history(raw)
    _validate_draft_preparation_state(raw)
    _validate_permit_status_state(raw)
    for field_name in (
        "requestStartedAt",
        "capabilityConsumedAt",
        "resolvedAt",
        "issuerSettledAt",
    ):
        field_time = _utc(raw.get(field_name))
        if field_time is not None and field_time < issued_at:
            raise GraphSendPermitError(
                f"Graph send permit {field_name} regressed before issuance"
            )
    if (
        _utc(raw.get("resolvedAt")) is not None
        and _utc(raw.get("requestStartedAt")) is not None
        and _utc(raw.get("resolvedAt")) < _utc(raw.get("requestStartedAt"))
    ):
        raise GraphSendPermitError(
            "Graph send permit provider resolution time regressed"
        )
    return dict(raw)


def _active_permit(transaction, thread_ref, thread_data: Dict[str, Any]):
    pointer = (thread_data or {}).get("activeGraphSendPermit")
    if pointer is None:
        return None, None
    if (
        not isinstance(pointer, dict)
        or pointer.get("version") != GRAPH_SEND_PERMIT_VERSION
        or not str(pointer.get("permitId") or "").strip()
        or not str(pointer.get("permitImmutableHash") or "").strip()
    ):
        raise GraphSendPermitBlocked(
            "active Graph send permit pointer is unresolved or malformed"
        )
    permit_ref = thread_ref.collection("graphSendPermits").document(
        pointer["permitId"]
    )
    permit_snapshot = permit_ref.get(transaction=transaction)
    if not permit_snapshot.exists:
        raise GraphSendPermitBlocked(
            "active Graph send permit document is unresolved or missing"
        )
    try:
        permit = _validate_permit(permit_snapshot.to_dict() or {})
    except GraphSendPermitError as exc:
        raise GraphSendPermitBlocked(
            f"active Graph send permit is unresolved or malformed: {exc}"
        ) from exc
    _require_permit_issuer_binding(thread_ref, permit)
    if (
        permit.get("permitId") != pointer.get("permitId")
        or permit.get("immutableHash") != pointer.get("permitImmutableHash")
    ):
        raise GraphSendPermitBlocked(
            "active Graph send permit pointer drifted from retained evidence"
        )
    _validate_settled_draft_review_document(
        permit,
        thread_ref,
        transaction=transaction,
    )
    return permit_ref, permit


def read_active_graph_send_permit(
    thread_ref,
    thread_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Read and strictly validate the permit named by one root snapshot.

    This read-only helper is for observability/completion gates that already
    loaded the root document.  A missing, malformed, or drifted retained
    permit fails closed instead of being mistaken for settled work.
    """
    pointer = (thread_data or {}).get("activeGraphSendPermit")
    if pointer is None:
        return None
    if (
        not isinstance(pointer, dict)
        or pointer.get("version") != GRAPH_SEND_PERMIT_VERSION
        or not str(pointer.get("permitId") or "").strip()
        or not str(pointer.get("permitImmutableHash") or "").strip()
    ):
        raise GraphSendPermitBlocked(
            "active Graph send permit pointer is unresolved or malformed"
        )
    permit_ref = thread_ref.collection("graphSendPermits").document(
        pointer["permitId"]
    )
    permit_snapshot = permit_ref.get()
    if not permit_snapshot.exists:
        raise GraphSendPermitBlocked(
            "active Graph send permit document is unresolved or missing"
        )
    try:
        permit = _validate_permit(permit_snapshot.to_dict() or {})
    except GraphSendPermitError as exc:
        raise GraphSendPermitBlocked(
            f"active Graph send permit is unresolved or malformed: {exc}"
        ) from exc
    _require_permit_issuer_binding(thread_ref, permit)
    if (
        permit.get("permitId") != pointer.get("permitId")
        or permit.get("immutableHash") != pointer.get("permitImmutableHash")
    ):
        raise GraphSendPermitBlocked(
            "active Graph send permit pointer drifted from retained evidence"
        )
    _validate_settled_draft_review_document(permit, thread_ref)
    return permit


def assert_terminal_staging_allowed(transaction, thread_ref) -> None:
    """Read pointer+retained doc inside the caller's staging transaction."""
    thread_snapshot = thread_ref.get(transaction=transaction)
    if not thread_snapshot.exists:
        raise GraphSendPermitBlocked("terminal staging thread root is missing")
    _permit_ref, permit = _active_permit(
        transaction,
        thread_ref,
        thread_snapshot.to_dict() or {},
    )
    if graph_send_permit_blocks_new_send(permit):
        raise GraphSendPermitBlocked(
            "active Graph send permit is unresolved or awaits operator draft "
            "review; terminal staging is blocked"
        )


def assert_pending_claim_allowed(
    transaction,
    thread_ref,
    *,
    thread_data: Optional[Dict[str, Any]] = None,
) -> None:
    """Prevent an expired queue lease from authorizing a second provider send.

    Queue ownership may be reclaimed only after the prior retained permit has
    reached an issuer-settled terminal status.  A provider request that is
    merely ``request_started`` (or accepted but not locally committed) stays a
    reconciliation-only state even after either lease expires.
    """
    if thread_data is None:
        thread_snapshot = thread_ref.get(transaction=transaction)
        if not thread_snapshot.exists:
            raise GraphSendPermitBlocked("pending claim thread root is missing")
        thread_data = thread_snapshot.to_dict() or {}
    _permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
    if graph_send_permit_blocks_new_send(permit):
        raise GraphSendPermitBlocked(
            "active Graph send permit is unresolved or awaits operator draft "
            "review; pending takeover is reconciliation-only"
        )


def _new_permit(
    *,
    permit_id: str,
    issuer_kind: str,
    issuer_owner: str,
    issuer_fence: Optional[int],
    issuer_document_id: Optional[str],
    issuer_document_path: Optional[str],
    thread_id: str,
    client_id: Optional[str],
    source_graph_message_id: Optional[str],
    conversation_id: Optional[str],
    recipient: str,
    body_hash: str,
    envelope_hash: str,
    capability_hash: str,
    now: datetime,
) -> Dict[str, Any]:
    immutable = {
        "version": GRAPH_SEND_PERMIT_VERSION,
        "permitId": permit_id,
        "issuerKind": issuer_kind,
        "issuerOwner": issuer_owner,
        "issuerFence": issuer_fence,
        "issuerDocumentId": issuer_document_id,
        "issuerDocumentPath": issuer_document_path,
        "threadId": thread_id,
        "clientId": client_id,
        "sourceGraphMessageId": source_graph_message_id,
        "conversationId": conversation_id,
        "recipient": str(recipient or "").strip().lower(),
        "bodyHash": body_hash,
        "envelopeHash": envelope_hash,
        "capabilityHash": capability_hash,
        "providerOperation": "graph_reply_send",
        "issuedAt": now,
        "leaseUntil": now + timedelta(seconds=GRAPH_SEND_PERMIT_LEASE_SECONDS),
        "providerDeadline": now + timedelta(
            seconds=GRAPH_SEND_PROVIDER_DEADLINE_SECONDS
        ),
    }
    permit = {
        **immutable,
        "immutableHash": _hash(immutable),
        "status": "issued",
    }
    return {**permit, **_initial_state_history(permit, now)}


def _issuer_document_identity(
    document_ref: Any,
    *,
    issuer_kind: str,
) -> tuple[str, str]:
    document_id = str(getattr(document_ref, "id", None) or "").strip()
    if not document_id or "/" in document_id:
        raise GraphSendPermitBlocked(
            "Graph send issuer document identity is missing or malformed"
        )
    document_path = _canonical_ref_path(document_ref)
    if document_path is None:
        # Firestore references always carry a path.  This deterministic prefix
        # exists only for the small in-memory unit doubles used by this module.
        collection_name = {
            "pending_response": "pendingResponses",
            "terminal_saga": "threads",
        }[issuer_kind]
        document_path = f"__in_memory__/{collection_name}/{document_id}"
    return document_id, document_path


def _pointer(permit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "version": GRAPH_SEND_PERMIT_VERSION,
        "permitId": permit["permitId"],
        "permitImmutableHash": permit["immutableHash"],
    }


def _validate_pending_claim(
    current: Dict[str, Any],
    loaded: Dict[str, Any],
    claim_token: str,
    *,
    now: datetime,
    require_active_lease: bool = True,
) -> str:
    expected_hash = pending_envelope_hash(loaded)
    if (
        pending_envelope_hash(current) != expected_hash
        or (current or {}).get("processingBy") != claim_token
    ):
        raise GraphSendPermitBlocked(
            "pending response claim token or immutable envelope changed"
        )
    lease_until = _utc((current or {}).get("processingLeaseUntil"))
    if require_active_lease and (lease_until is None or lease_until <= now):
        raise GraphSendPermitBlocked(
            "pending response claim lease is missing, expired, or malformed"
        )
    return expected_hash


_TERMINAL_UNISSUED_REPLY_ATTEMPT_FIELDS = frozenset({
    "sagaKey",
    "sourceMessageKey",
    "sourceGraphMessageId",
    "conversationId",
    "recipient",
    "responseBodyHash",
    "status",
    "startedAt",
})


def _canonical_terminal_response_body(saga: Dict[str, Any]) -> str:
    response_body = str((saga or {}).get("responseBody") or "").strip()
    if not response_body:
        raise GraphSendPermitBlocked(
            "terminal unissued reply attempt has no nonempty response body"
        )
    return response_body


def validate_unissued_terminal_reply_attempt(
    saga: Dict[str, Any],
    attempt: Dict[str, Any],
) -> Dict[str, Any]:
    """Validate the only durable terminal intent eligible for first issuance."""
    if (
        not isinstance(attempt, dict)
        or set(attempt) != _TERMINAL_UNISSUED_REPLY_ATTEMPT_FIELDS
    ):
        raise GraphSendPermitBlocked(
            "terminal unissued reply attempt schema is missing or contains "
            "unexpected fields"
        )
    required_saga_fields = (
        "sagaKey",
        "sourceMessageKey",
        "sourceGraphMessageId",
        "sourceConversationId",
        "replyRecipient",
    )
    if any(
        not isinstance((saga or {}).get(field_name), str)
        or not (saga or {}).get(field_name).strip()
        for field_name in required_saga_fields
    ):
        raise GraphSendPermitBlocked(
            "terminal unissued reply attempt has malformed immutable saga bindings"
        )
    expected_recipient = str(saga["replyRecipient"]).strip().lower()
    response_body_hash = _body_hash(_canonical_terminal_response_body(saga))
    if attempt.get("responseBodyHash") != response_body_hash:
        raise GraphSendPermitBlocked(
            "terminal reply attempt body hash does not match immutable saga "
            "response body"
        )
    expected = {
        "sagaKey": saga["sagaKey"],
        "sourceMessageKey": saga["sourceMessageKey"],
        "sourceGraphMessageId": saga["sourceGraphMessageId"],
        "conversationId": saga["sourceConversationId"],
        "recipient": expected_recipient,
        "status": "sending",
    }
    if any(attempt.get(field_name) != value for field_name, value in expected.items()):
        raise GraphSendPermitBlocked(
            "terminal unissued reply attempt drifted from immutable saga intent"
        )
    started_at = attempt.get("startedAt")
    try:
        aware_started_at = bool(
            isinstance(started_at, datetime)
            and started_at.tzinfo is not None
            and started_at.utcoffset() is not None
        )
    except Exception:
        aware_started_at = False
    if not aware_started_at:
        raise GraphSendPermitBlocked(
            "terminal unissued reply attempt startedAt is not an aware datetime"
        )
    return dict(attempt)


def issue_pending_graph_send_permit(
    firestore_client,
    thread_ref,
    pending_ref,
    loaded_data: Dict[str, Any],
    claim_token: str,
) -> Optional[GraphSendCapability]:
    """Linearize a pending worker against terminal staging."""
    transaction = firestore_client.transaction()
    now = datetime.now(timezone.utc)
    thread_snapshot = thread_ref.get(transaction=transaction)
    pending_snapshot = pending_ref.get(transaction=transaction)
    if not thread_snapshot.exists or not pending_snapshot.exists:
        return None
    thread_data = thread_snapshot.to_dict() or {}
    pending_data = pending_snapshot.to_dict() or {}
    canonical_thread_id = getattr(thread_ref, "id", None)
    loaded_thread_id = (loaded_data or {}).get("threadId")
    if (
        not isinstance(canonical_thread_id, str)
        or not canonical_thread_id
        or not isinstance(loaded_thread_id, str)
        or loaded_thread_id != canonical_thread_id
    ):
        raise GraphSendPermitBlocked(
            "pending Graph permit source threadId does not match its canonical "
            "thread issuer"
        )
    issuer_document_id, issuer_document_path = _issuer_document_identity(
        pending_ref,
        issuer_kind="pending_response",
    )
    _require_canonical_ref_path(
        pending_ref,
        _pending_response_path(thread_ref, issuer_document_id),
        label="pending permit issuer reference",
    )
    envelope_hash = _validate_pending_claim(
        pending_data,
        loaded_data,
        claim_token,
        now=now,
    )
    if has_terminal_send_marker(thread_data):
        transaction.update(pending_ref, {
            "status": "queued",
            "processingBy": None,
            "processingAt": None,
            "processingLeaseUntil": None,
            "updatedAt": SERVER_TIMESTAMP,
        })
        transaction.commit()
        return None

    _prior_ref, prior = _active_permit(transaction, thread_ref, thread_data)
    if graph_send_permit_blocks_new_send(prior):
        raise GraphSendPermitBlocked(
            "prior active Graph send permit is unresolved or awaits operator "
            "draft review; no new permit is allowed"
        )

    permit_id = f"graph-send-{uuid4().hex}"
    plaintext_capability = uuid4().hex
    permit = _new_permit(
        permit_id=permit_id,
        issuer_kind="pending_response",
        issuer_owner=claim_token,
        issuer_fence=None,
        issuer_document_id=issuer_document_id,
        issuer_document_path=issuer_document_path,
        thread_id=canonical_thread_id,
        client_id=(loaded_data or {}).get("clientId"),
        source_graph_message_id=(loaded_data or {}).get("msgId"),
        conversation_id=(loaded_data or {}).get("conversationId"),
        recipient=(loaded_data or {}).get("recipient"),
        body_hash=_body_hash((loaded_data or {}).get("responseBody")),
        envelope_hash=envelope_hash,
        capability_hash=hashlib.sha256(
            plaintext_capability.encode("utf-8")
        ).hexdigest(),
        now=now,
    )
    permit_ref = thread_ref.collection("graphSendPermits").document(permit_id)
    expected_pointer = _pointer(permit)
    thread_patch = {
        "activeGraphSendPermit": _pointer(permit),
        "updatedAt": SERVER_TIMESTAMP,
    }
    pending_patch = {
        "graphSendPermitId": permit_id,
        "graphSendPermitHash": permit["immutableHash"],
        "processingLeaseUntil": permit["leaseUntil"],
        "updatedAt": SERVER_TIMESTAMP,
    }
    capability = GraphSendCapability(
        permit_id=permit_id,
        immutable_hash=permit["immutableHash"],
        issuer_kind="pending_response",
        issuer_owner=claim_token,
        issuer_fence=None,
        envelope_hash=envelope_hash,
        capability=plaintext_capability,
        firestore_client=firestore_client,
        thread_ref=thread_ref,
        permit_ref=permit_ref,
        issuer_ref=pending_ref,
    )
    original_thread_data = dict(thread_data)
    original_pending_data = dict(pending_data)
    expected_thread_data = {
        **original_thread_data,
        "activeGraphSendPermit": expected_pointer,
    }
    expected_pending_data = {
        **original_pending_data,
        "graphSendPermitId": permit_id,
        "graphSendPermitHash": permit["immutableHash"],
        "processingLeaseUntil": permit["leaseUntil"],
    }

    def classify_issue_readback(
        thread_readback,
        pending_readback,
        permit_readback,
    ) -> str:
        thread_exists = getattr(thread_readback, "exists", None)
        pending_exists = getattr(pending_readback, "exists", None)
        permit_exists = getattr(permit_readback, "exists", None)
        raw_thread = (
            thread_readback.to_dict() if thread_exists is True else None
        )
        raw_pending = (
            pending_readback.to_dict() if pending_exists is True else None
        )
        if (
            thread_exists is not True
            or pending_exists is not True
            or not isinstance(raw_thread, dict)
            or not isinstance(raw_pending, dict)
        ):
            return "drift"
        if (
            permit_exists is False
            and raw_thread == original_thread_data
            and raw_pending == original_pending_data
        ):
            return "source"
        if permit_exists is not True:
            return "drift"
        raw_permit = permit_readback.to_dict()
        try:
            readback_permit = _validate_permit(raw_permit)
        except GraphSendPermitError:
            return "drift"
        if (
            readback_permit == permit
            and _terminal_thread_commit_comparable(raw_thread)
            == _terminal_thread_commit_comparable(expected_thread_data)
            and _pending_settlement_commit_comparable(raw_pending)
            == _pending_settlement_commit_comparable(expected_pending_data)
            and raw_thread.get("activeGraphSendPermit") == expected_pointer
            and raw_pending.get("graphSendPermitId") == permit_id
            and raw_pending.get("graphSendPermitHash")
            == permit["immutableHash"]
            and raw_pending.get("processingLeaseUntil")
            == permit["leaseUntil"]
        ):
            return "target"
        return "drift"

    def issue_readback_state() -> str:
        readback_transaction = firestore_client.transaction()
        return classify_issue_readback(
            thread_ref.get(transaction=readback_transaction),
            pending_ref.get(transaction=readback_transaction),
            permit_ref.get(transaction=readback_transaction),
        )

    def enqueue_issue_writes(issue_transaction) -> None:
        issue_transaction.set(permit_ref, permit)
        issue_transaction.update(thread_ref, thread_patch)
        issue_transaction.update(pending_ref, pending_patch)

    def enqueue_exact_issue_retry():
        retry_transaction = firestore_client.transaction()
        retry_state = classify_issue_readback(
            thread_ref.get(transaction=retry_transaction),
            pending_ref.get(transaction=retry_transaction),
            permit_ref.get(transaction=retry_transaction),
        )
        if retry_state != "source":
            raise GraphSendPermitError(
                "pending Graph permit issuance retry lost its exact source"
            )
        enqueue_issue_writes(retry_transaction)
        return retry_transaction

    enqueue_issue_writes(transaction)
    _commit_exact_orphaned_draft_settlement(
        transaction,
        readback_state=issue_readback_state,
        enqueue_retry=enqueue_exact_issue_retry,
        operation="pending Graph permit issuance",
    )
    return capability


def issue_terminal_graph_send_permit(
    firestore_client,
    thread_ref,
    claim_ref,
    saga: Dict[str, Any],
    issuer_owner: str,
    issuer_fence: int,
) -> GraphSendCapability:
    """Issue a terminal acknowledgement permit under the saga owner/fence."""
    transaction = firestore_client.transaction()
    now = datetime.now(timezone.utc)
    same_claim_root = _same_document_ref(claim_ref, thread_ref)
    thread_snapshot = thread_ref.get(transaction=transaction)
    claim_snapshot = (
        thread_snapshot
        if same_claim_root
        else claim_ref.get(transaction=transaction)
    )
    if not thread_snapshot.exists or not claim_snapshot.exists:
        raise GraphSendPermitBlocked("terminal Graph permit roots are missing")
    thread_data = thread_snapshot.to_dict() or {}
    claim_data = claim_snapshot.to_dict() or {}
    issuer_document_id, issuer_document_path = _issuer_document_identity(
        claim_ref,
        issuer_kind="terminal_saga",
    )
    planned_claim_document_id = str(
        (saga.get("finalizationPlan") or {}).get("claimThreadId") or ""
    ).strip()
    user_path = _thread_user_root_path(thread_ref)
    _require_canonical_ref_path(
        claim_ref,
        (
            f"{user_path}/threads/{issuer_document_id}"
            if user_path is not None
            else None
        ),
        label="terminal permit issuer reference",
    )
    claim = claim_data.get("terminalSagaClaim")
    attempt = thread_data.get("terminalReplyAttempt")
    if (
        not isinstance(claim, dict)
        or claim.get("sagaKey") != saga.get("sagaKey")
        or claim.get("immutableHash") != saga.get("immutableHash")
        or claim.get("owner") != issuer_owner
        or claim.get("fencingToken") != issuer_fence
        or planned_claim_document_id != issuer_document_id
        or _utc(claim.get("leaseUntil")) is None
        or _utc(claim.get("leaseUntil")) <= now
    ):
        raise GraphSendPermitBlocked(
            "terminal saga issuer ownership changed before Graph permit"
        )
    if not thread_data.get("terminalReplyOwed"):
        raise GraphSendPermitBlocked(
            "terminal reply intent changed before Graph permit"
        )
    try:
        validate_unissued_terminal_reply_attempt(saga, attempt)
    except GraphSendPermitBlocked as exc:
        raise GraphSendPermitBlocked(
            f"terminal reply intent changed before Graph permit: {exc}"
        ) from exc
    body_hash = _body_hash(_canonical_terminal_response_body(saga))
    _prior_ref, prior = _active_permit(transaction, thread_ref, thread_data)
    if graph_send_permit_blocks_new_send(prior):
        raise GraphSendPermitBlocked(
            "prior active Graph send permit is unresolved or awaits operator "
            "draft review; terminal send is blocked"
        )

    envelope = {
        "sagaKey": saga.get("sagaKey"),
        "sagaImmutableHash": saga.get("immutableHash"),
        "sourceGraphMessageId": saga.get("sourceGraphMessageId"),
        "conversationId": saga.get("sourceConversationId"),
        "recipient": str(saga.get("replyRecipient") or "").strip().lower(),
        "bodyHash": body_hash,
    }
    envelope_hash = _hash(envelope)
    permit_id = f"graph-send-{uuid4().hex}"
    plaintext_capability = uuid4().hex
    permit = _new_permit(
        permit_id=permit_id,
        issuer_kind="terminal_saga",
        issuer_owner=issuer_owner,
        issuer_fence=issuer_fence,
        issuer_document_id=issuer_document_id,
        issuer_document_path=issuer_document_path,
        thread_id=str(getattr(thread_ref, "id", None) or ""),
        client_id=saga.get("clientId"),
        source_graph_message_id=saga.get("sourceGraphMessageId"),
        conversation_id=saga.get("sourceConversationId"),
        recipient=saga.get("replyRecipient"),
        body_hash=body_hash,
        envelope_hash=envelope_hash,
        capability_hash=hashlib.sha256(
            plaintext_capability.encode("utf-8")
        ).hexdigest(),
        now=now,
    )
    permit_ref = thread_ref.collection("graphSendPermits").document(permit_id)
    transaction.set(permit_ref, permit)
    expected_pointer = _pointer(permit)
    expected_attempt = {
        **attempt,
        "graphSendPermitId": permit_id,
        "graphSendPermitHash": permit["immutableHash"],
    }
    thread_patch = {
        "activeGraphSendPermit": expected_pointer,
        "terminalReplyAttempt": expected_attempt,
        "updatedAt": SERVER_TIMESTAMP,
    }
    transaction.update(thread_ref, thread_patch)
    capability = GraphSendCapability(
        permit_id=permit_id,
        immutable_hash=permit["immutableHash"],
        issuer_kind="terminal_saga",
        issuer_owner=issuer_owner,
        issuer_fence=issuer_fence,
        envelope_hash=envelope_hash,
        capability=plaintext_capability,
        firestore_client=firestore_client,
        thread_ref=thread_ref,
        permit_ref=permit_ref,
        issuer_ref=claim_ref,
    )
    original_thread_data = dict(thread_data)
    original_claim_data = dict(claim_data)
    original_claim = dict(claim)

    try:
        transaction.commit()
    except Exception as commit_error:
        try:
            permit_readback = permit_ref.get()
            thread_readback = thread_ref.get()
            claim_readback = (
                thread_readback if same_claim_root else claim_ref.get()
            )
            permit_exists = getattr(permit_readback, "exists", None)
            thread_exists = getattr(thread_readback, "exists", None)
            claim_exists = getattr(claim_readback, "exists", None)
            readback_permit = None
            if permit_exists is True:
                raw_permit = permit_readback.to_dict()
                if not isinstance(raw_permit, dict):
                    raise GraphSendPermitError(
                        "terminal Graph permit issuance readback is malformed"
                    )
                readback_permit = _validate_permit(raw_permit)
            readback_thread_data = (
                thread_readback.to_dict() if thread_exists is True else None
            )
            readback_claim_data = (
                claim_readback.to_dict() if claim_exists is True else None
            )
            if (
                thread_exists is True
                and not isinstance(readback_thread_data, dict)
            ) or (
                claim_exists is True
                and not isinstance(readback_claim_data, dict)
            ):
                raise GraphSendPermitError(
                    "terminal Graph permit issuance root readback is malformed"
                )
        except Exception as readback_error:
            raise GraphSendPermitError(
                "terminal Graph permit issuance commit outcome is ambiguous; "
                "exact readback was unavailable"
            ) from readback_error

        expected_thread_data = {
            **original_thread_data,
            "activeGraphSendPermit": expected_pointer,
            "terminalReplyAttempt": expected_attempt,
        }
        comparable_thread_data = (
            {
                key: value
                for key, value in readback_thread_data.items()
                if key != "updatedAt"
            }
            if isinstance(readback_thread_data, dict)
            else None
        )
        comparable_expected_thread_data = {
            key: value
            for key, value in expected_thread_data.items()
            if key != "updatedAt"
        }
        readback_claim = (
            readback_claim_data.get("terminalSagaClaim")
            if isinstance(readback_claim_data, dict)
            else None
        )
        claim_lease_until = (
            _utc(readback_claim.get("leaseUntil"))
            if isinstance(readback_claim, dict)
            else None
        )
        separate_claim_root_unchanged = (
            same_claim_root or readback_claim_data == original_claim_data
        )
        applied_exactly = (
            permit_exists is True
            and thread_exists is True
            and claim_exists is True
            and readback_permit == permit
            and comparable_thread_data == comparable_expected_thread_data
            and "updatedAt" in readback_thread_data
            and readback_thread_data.get("activeGraphSendPermit")
            == expected_pointer
            and readback_thread_data.get("terminalReplyAttempt")
            == expected_attempt
            and readback_claim == original_claim
            and separate_claim_root_unchanged
            and readback_permit.get("issuerDocumentId")
            == issuer_document_id
            and readback_permit.get("issuerDocumentPath")
            == issuer_document_path
            and readback_claim.get("sagaKey") == saga.get("sagaKey")
            and readback_claim.get("immutableHash")
            == saga.get("immutableHash")
            and readback_claim.get("owner") == issuer_owner
            and readback_claim.get("fencingToken") == issuer_fence
            and claim_lease_until is not None
            and claim_lease_until > datetime.now(timezone.utc)
        )
        if applied_exactly:
            return capability

        definitely_not_applied = (
            permit_exists is False
            and thread_exists is True
            and claim_exists is True
            and readback_thread_data == original_thread_data
            and (
                same_claim_root
                or readback_claim_data == original_claim_data
            )
            and (
                ("activeGraphSendPermit" in readback_thread_data)
                == ("activeGraphSendPermit" in original_thread_data)
            )
            and readback_thread_data.get("activeGraphSendPermit")
            == original_thread_data.get("activeGraphSendPermit")
            and readback_thread_data.get("terminalReplyAttempt")
            == original_thread_data.get("terminalReplyAttempt")
            and readback_claim == original_claim
        )
        if definitely_not_applied:
            raise GraphSendPermitError(
                "terminal Graph permit issuance did not commit; retry may "
                "safely issue one new permit"
            ) from commit_error
        raise GraphSendPermitError(
            "terminal Graph permit issuance commit outcome is ambiguous; "
            "exact readback did not match either atomic outcome"
        ) from commit_error

    return capability


def _read_exact_capability_permit(
    transaction,
    capability: GraphSendCapability,
) -> Dict[str, Any]:
    thread_snapshot = capability.thread_ref.get(transaction=transaction)
    permit_snapshot = capability.permit_ref.get(transaction=transaction)
    if not thread_snapshot.exists or not permit_snapshot.exists:
        raise GraphSendPermitBlocked("Graph send capability evidence is missing")
    pointer = (thread_snapshot.to_dict() or {}).get("activeGraphSendPermit")
    permit = _validate_permit(permit_snapshot.to_dict() or {})
    issuer_document_id, issuer_document_path = _issuer_document_identity(
        capability.issuer_ref,
        issuer_kind=capability.issuer_kind,
    )
    if (
        not isinstance(pointer, dict)
        or pointer.get("permitId") != capability.permit_id
        or pointer.get("permitImmutableHash") != capability.immutable_hash
        or permit.get("permitId") != capability.permit_id
        or permit.get("immutableHash") != capability.immutable_hash
        or permit.get("issuerOwner") != capability.issuer_owner
        or permit.get("issuerFence") != capability.issuer_fence
        or permit.get("issuerKind") != capability.issuer_kind
        or permit.get("issuerDocumentId") != issuer_document_id
        or permit.get("issuerDocumentPath") != issuer_document_path
        or permit.get("threadId")
        != str(getattr(capability.thread_ref, "id", None) or "")
        or permit.get("envelopeHash") != capability.envelope_hash
        or permit.get("capabilityHash")
        != hashlib.sha256(capability.capability.encode("utf-8")).hexdigest()
    ):
        raise GraphSendPermitBlocked(
            "Graph send capability or active pointer changed"
        )
    _validate_settled_draft_review_document(
        permit,
        capability.thread_ref,
        transaction=transaction,
    )
    return permit


def _validate_capability_issuer(
    transaction,
    capability: GraphSendCapability,
    *,
    now: datetime,
    require_active_lease: bool,
) -> Dict[str, Any]:
    issuer_snapshot = capability.issuer_ref.get(transaction=transaction)
    issuer_data = issuer_snapshot.to_dict() if issuer_snapshot.exists else {}
    if capability.issuer_kind == "pending_response":
        lease_until = _utc(issuer_data.get("processingLeaseUntil"))
        if (
            not issuer_snapshot.exists
            or issuer_data.get("processingBy") != capability.issuer_owner
            or issuer_data.get("graphSendPermitId") != capability.permit_id
            or issuer_data.get("graphSendPermitHash") != capability.immutable_hash
            or pending_envelope_hash(issuer_data) != capability.envelope_hash
            or (
                require_active_lease
                and (lease_until is None or lease_until <= now)
            )
        ):
            raise GraphSendPermitBlocked(
                "pending issuer changed before Graph draft/send operation"
            )
    else:
        claim = issuer_data.get("terminalSagaClaim")
        lease_until = _utc((claim or {}).get("leaseUntil"))
        thread_snapshot = capability.thread_ref.get(transaction=transaction)
        thread_data = thread_snapshot.to_dict() if thread_snapshot.exists else {}
        attempt = (thread_data or {}).get("terminalReplyAttempt")
        if (
            not isinstance(claim, dict)
            or claim.get("owner") != capability.issuer_owner
            or claim.get("fencingToken") != capability.issuer_fence
            or not isinstance(attempt, dict)
            or attempt.get("graphSendPermitId") != capability.permit_id
            or attempt.get("graphSendPermitHash") != capability.immutable_hash
            or (
                require_active_lease
                and (lease_until is None or lease_until <= now)
            )
        ):
            raise GraphSendPermitBlocked(
                "terminal issuer changed before Graph draft/send operation"
            )
    return issuer_data


def _remaining_provider_seconds(
    permit: Dict[str, Any],
    now: datetime,
    *,
    maximum: float = GRAPH_SEND_HTTP_MAX_SECONDS,
) -> float:
    lease_until = _utc(permit.get("leaseUntil"))
    provider_deadline = _utc(permit.get("providerDeadline"))
    remaining = min(
        (lease_until - now).total_seconds(),
        (provider_deadline - now).total_seconds(),
        float(maximum),
    )
    if remaining < GRAPH_SEND_HTTP_MIN_SECONDS:
        raise GraphSendPermitBlocked(
            "Graph provider plan expired before provider mutation"
        )
    return remaining


def is_expired_orphaned_graph_draft_request(
    raw_permit: Dict[str, Any],
) -> bool:
    """Identify an expired pre-send request intent that cannot be replayed."""
    permit = _validate_permit(raw_permit)
    preparation = dict(permit.get("draftPreparation") or {})
    lease_until = _utc(permit.get("leaseUntil"))
    return bool(
        permit.get("status") == "issued"
        and permit.get("requestStartedAt") is None
        and preparation.get("state") in _ORPHANED_DRAFT_REQUEST_EVENTS
        and lease_until is not None
        and lease_until <= datetime.now(timezone.utc)
    )


def _orphaned_graph_draft_reconciliation_patch(
    raw_permit: Dict[str, Any],
    *,
    now: datetime,
) -> tuple[Dict[str, Any], Dict[str, Any], str]:
    """Build one legal unknown-draft transition without a capability secret."""
    permit = _validate_permit(raw_permit)
    preparation = dict(permit.get("draftPreparation") or {})
    request_state = preparation.get("state")
    operation = _ORPHANED_DRAFT_REQUEST_EVENTS.get(request_state)
    lease_until = _utc(permit.get("leaseUntil"))
    history = list(permit.get("stateHistory") or [])
    if (
        permit.get("status") != "issued"
        or permit.get("requestStartedAt") is not None
        or operation is None
        or lease_until is None
        or lease_until > now
        or len(history) + 2 > _PERMIT_STATE_HISTORY_LIMIT
    ):
        raise GraphSendPermitBlocked(
            "Graph draft request is not an expired recoverable orphan"
        )
    event, phase, label = operation
    draft_id = str(preparation.get("draftId") or "").strip() or None
    evidence = {
        "reason": (
            f"{label} was orphaned after its retained permit lease expired; "
            "the provider draft-mutation outcome is unknown and automatic "
            "replay is forbidden"
        ),
        "phase": phase,
        "draftId": draft_id,
        "providerSendStarted": False,
        "automaticDeleteAttempted": False,
    }
    evidence_hash = _hash(evidence)
    updated_preparation = {
        **preparation,
        "state": "draft_mutation_needs_reconciliation",
    }
    if request_state == "create_request_started":
        updated_preparation.update({
            "draftId": None,
            "createOutcome": "needs_reconciliation",
            "createOutcomeAt": now,
            "createOutcomeEvidence": evidence,
            "createOutcomeEvidenceHash": evidence_hash,
        })
    elif request_state == "patch_request_started":
        updated_preparation.update({
            "patchOutcome": "needs_reconciliation",
            "patchOutcomeAt": now,
            "patchOutcomeEvidence": evidence,
            "patchOutcomeEvidenceHash": evidence_hash,
        })
    else:
        active = dict(preparation.get("activeAttachment") or {})
        attachment_index = active.get("index")
        if type(attachment_index) is not int or attachment_index < 0:
            raise GraphSendPermitBlocked(
                "orphaned Graph attachment request has malformed active evidence"
            )
        event = f"draft_attachment_{attachment_index}_needs_reconciliation"
        updated_preparation.update({
            "attachmentOutcomeEvidence": evidence,
            "attachmentOutcomeEvidenceHash": evidence_hash,
        })
    permit_patch = {
        "status": "needs_reconciliation",
        "draftPreparation": updated_preparation,
        "resolvedAt": now,
        "resolutionEvidence": evidence,
        "resolutionEvidenceHash": evidence_hash,
        "updatedAt": SERVER_TIMESTAMP,
    }
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event=event,
        now=now,
    )
    return {**permit, **state_patch}, state_patch, event


def expired_graph_send_pre_send_recovery_kind(
    raw_permit: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Classify an expired permit that can be settled without provider work."""
    permit = _validate_permit(raw_permit)
    observed_at = now or datetime.now(timezone.utc)
    lease_until = _utc(permit.get("leaseUntil"))
    if (
        permit.get("requestStartedAt") is not None
        or lease_until is None
        or lease_until > observed_at
    ):
        return None
    preparation = dict(permit.get("draftPreparation") or {})
    state = preparation.get("state")
    if permit.get("status") == "issued":
        if not preparation:
            return "definitely_not_started"
        if state in (
            _RETAINABLE_PRE_SEND_DRAFT_STATES
            | set(_ORPHANED_DRAFT_REQUEST_EVENTS)
        ):
            return "draft_needs_review"
    if (
        permit.get("status") == "definitely_not_sent"
        and state == "create_definitely_not_created"
        and _exact_definitely_not_created_resolution(permit)
    ):
        return "definitely_not_sent"
    if (
        permit.get("status") == "definitely_not_sent"
        and not preparation
        and _exact_preflight_definitely_not_sent_resolution(permit)
    ):
        return "definitely_not_sent"
    return None


def _exact_definitely_not_created_resolution(
    raw_permit: Dict[str, Any],
) -> bool:
    """Require the exact durable create outcome that proves no draft exists."""
    permit = _validate_permit(raw_permit)
    preparation = dict(permit.get("draftPreparation") or {})
    evidence = permit.get("resolutionEvidence")
    return bool(
        permit.get("status") == "definitely_not_sent"
        and permit.get("requestStartedAt") is None
        and preparation.get("state") == "create_definitely_not_created"
        and preparation.get("draftId") is None
        and preparation.get("createOutcome") == "definitely_not_created"
        and _utc(permit.get("resolvedAt"))
        == _utc(preparation.get("createOutcomeAt"))
        and isinstance(evidence, dict)
        and evidence == preparation.get("createOutcomeEvidence")
        and permit.get("resolutionEvidenceHash")
        == preparation.get("createOutcomeEvidenceHash")
    )


def _exact_preflight_definitely_not_sent_resolution(
    raw_permit: Dict[str, Any],
) -> bool:
    """Require typed no-draft evidence that provider work never started."""
    permit = _validate_permit(raw_permit)
    evidence = permit.get("resolutionEvidence")
    return bool(
        permit.get("status") == "definitely_not_sent"
        and permit.get("requestStartedAt") is None
        and not permit.get("draftPreparation")
        and _utc(permit.get("resolvedAt")) is not None
        and isinstance(evidence, dict)
        and permit.get("resolutionEvidenceHash") == _hash(evidence)
        and str(evidence.get("phase") or "").strip()
    )


def _lost_capability_pre_send_projection_patch(
    raw_permit: Dict[str, Any],
    *,
    now: datetime,
    outcome: str,
) -> tuple[Dict[str, Any], Dict[str, Any], str]:
    """Project one expired issued permit to a legal provider-resolution state."""
    permit = _validate_permit(raw_permit)
    preparation = dict(permit.get("draftPreparation") or {})
    state = preparation.get("state")
    lease_until = _utc(permit.get("leaseUntil"))
    history = list(permit.get("stateHistory") or [])
    if (
        permit.get("status") != "issued"
        or permit.get("requestStartedAt") is not None
        or lease_until is None
        or lease_until > now
        or len(history) + 2 > _PERMIT_STATE_HISTORY_LIMIT
    ):
        raise GraphSendPermitBlocked(
            "Graph pre-send capability loss is not an expired recoverable source"
        )
    if outcome == "draft_needs_review" and state in (
        _ORPHANED_DRAFT_REQUEST_EVENTS
    ):
        return _orphaned_graph_draft_reconciliation_patch(
            permit,
            now=now,
        )
    if (
        outcome == "draft_needs_review"
        and state in _RETAINABLE_PRE_SEND_DRAFT_STATES
    ):
        evidence = {
            "reason": (
                "The retained Graph draft capability expired before /send; "
                "automatic replay or deletion is forbidden"
            ),
            "phase": "lost_pre_send_capability",
            "draftId": str(preparation.get("draftId") or "").strip() or None,
            "providerSendStarted": False,
            "automaticDeleteAttempted": False,
        }
        event = "provider_needs_reconciliation"
        permit_patch = {
            "status": "needs_reconciliation",
            "resolvedAt": now,
            "resolutionEvidence": evidence,
            "resolutionEvidenceHash": _hash(evidence),
            "updatedAt": SERVER_TIMESTAMP,
        }
    elif outcome == "definitely_not_started" and not preparation:
        evidence = {
            "reason": (
                "The retained Graph permit expired before any provider "
                "mutation started"
            ),
            "phase": "lost_pre_send_capability",
            "draftId": None,
            "providerSendStarted": False,
            "automaticDeleteAttempted": False,
        }
        event = "provider_definitely_not_sent"
        permit_patch = {
            "status": "definitely_not_sent",
            "resolvedAt": now,
            "resolutionEvidence": evidence,
            "resolutionEvidenceHash": _hash(evidence),
            "updatedAt": SERVER_TIMESTAMP,
        }
    else:
        raise GraphSendPermitBlocked(
            "Graph pre-send capability loss does not match its settlement outcome"
        )
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event=event,
        now=now,
    )
    return {**permit, **state_patch}, state_patch, event


def _orphaned_draft_review_payload(
    permit: Dict[str, Any],
    raw_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Bind server-owned review content to the projected orphan resolution."""
    preparation = dict(permit.get("draftPreparation") or {})
    resolution_evidence = dict(permit.get("resolutionEvidence") or {})
    envelope = dict(permit.get("preparedEnvelope") or {})
    return {
        **dict(raw_payload or {}),
        "failureReason": resolution_evidence.get("reason"),
        "sourceGraphMessageId": permit.get("sourceGraphMessageId"),
        "preparedEnvelopeHash": envelope.get("preparedEnvelopeHash"),
        "draftId": preparation.get("draftId"),
        "draftMutationState": preparation.get("state"),
        "draftResolutionEvidenceHash": permit.get(
            "resolutionEvidenceHash"
        ),
        "automaticDeleteAttempted": False,
    }


def _local_transition_comparable(permit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in dict(permit or {}).items()
        if key != "updatedAt"
    }


def _read_exact_local_transition_state(
    capability: GraphSendCapability,
    *,
    source_permit: Dict[str, Any],
    target_permit: Dict[str, Any],
    operation: str,
):
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    try:
        readback = _read_exact_capability_permit(transaction, capability)
        _validate_capability_issuer(
            transaction,
            capability,
            now=now,
            require_active_lease=True,
        )
    except Exception as readback_error:
        raise GraphSendPermitError(
            f"{operation} commit outcome is ambiguous; exact readback is malformed"
        ) from readback_error
    if _local_transition_comparable(readback) == _local_transition_comparable(
        target_permit
    ):
        return "target", transaction, readback, now
    if readback == source_permit:
        return "source", transaction, readback, now
    raise GraphSendPermitError(
        f"{operation} commit outcome is ambiguous; exact readback drifted"
    )


def _commit_exact_local_transition(
    capability: GraphSendCapability,
    transaction,
    source_permit: Dict[str, Any],
    state_patch: Dict[str, Any],
    *,
    operation: str,
    timeout_seconds: Optional[float] = None,
):
    """Commit one local pre-provider transition with bounded exact recovery."""
    target_permit = {**source_permit, **dict(state_patch)}
    transaction.update(capability.permit_ref, state_patch)

    def recovered_result(readback: Dict[str, Any], now: datetime):
        recovered_timeout = timeout_seconds
        if timeout_seconds is not None:
            recovered_timeout = _remaining_provider_seconds(
                readback,
                now,
                maximum=float(timeout_seconds),
            )
        return readback, recovered_timeout

    try:
        transaction.commit()
        return target_permit, timeout_seconds
    except Exception as first_error:
        state, retry_transaction, readback, readback_at = (
            _read_exact_local_transition_state(
                capability,
                source_permit=source_permit,
                target_permit=target_permit,
                operation=operation,
            )
        )
        if state == "target":
            return recovered_result(readback, readback_at)

        retry_transaction.update(
            capability.permit_ref,
            state_patch,
        )
        try:
            retry_transaction.commit()
        except Exception as retry_error:
            retry_state, _transaction, retry_readback, retry_readback_at = (
                _read_exact_local_transition_state(
                    capability,
                    source_permit=source_permit,
                    target_permit=target_permit,
                    operation=operation,
                )
            )
            if retry_state == "target":
                return recovered_result(retry_readback, retry_readback_at)
            raise GraphSendPermitLocalRetryable(
                f"{operation} did not commit after one exact retry; "
                "the unchanged local source state remains retryable"
            ) from retry_error

        retry_state, _transaction, retry_readback, retry_readback_at = (
            _read_exact_local_transition_state(
                capability,
                source_permit=source_permit,
                target_permit=target_permit,
                operation=operation,
            )
        )
        if retry_state != "target":
            raise GraphSendPermitLocalRetryable(
                f"{operation} exact retry returned without committing; "
                "the unchanged local source state remains retryable"
            ) from first_error
        return recovered_result(retry_readback, retry_readback_at)


def _commit_exact_orphaned_draft_settlement(
    transaction,
    *,
    readback_state,
    enqueue_retry,
    operation: str,
) -> None:
    """Recover one atomic orphan review commit without replaying provider work."""
    try:
        transaction.commit()
        return
    except Exception as first_error:
        state = readback_state()
        if state == "target":
            return
        if state != "source":
            raise GraphSendPermitError(
                f"{operation} commit outcome drifted from exact source/target"
            ) from first_error

        retry_transaction = enqueue_retry()
        try:
            retry_transaction.commit()
        except Exception as retry_error:
            retry_state = readback_state()
            if retry_state == "target":
                return
            if retry_state == "source":
                raise GraphSendPermitLocalRetryable(
                    f"{operation} did not commit after one exact retry"
                ) from retry_error
            raise GraphSendPermitError(
                f"{operation} retry outcome drifted from exact source/target"
            ) from retry_error
        if readback_state() != "target":
            raise GraphSendPermitLocalRetryable(
                f"{operation} exact retry returned without its full target"
            ) from first_error


def _terminal_thread_commit_comparable(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Omit only the non-semantic root update timestamp from CAS readback."""
    comparable = dict(raw or {})
    comparable.pop("updatedAt", None)
    return comparable


def _pending_settlement_commit_comparable(
    raw: Dict[str, Any],
    *,
    created_at_server_owned: bool = False,
) -> Dict[str, Any]:
    """Omit only Firestore-resolved timestamps written by pending settlement."""
    comparable = dict(raw or {})
    if created_at_server_owned:
        comparable.pop("createdAt", None)
    comparable.pop("updatedAt", None)
    return comparable


def _enqueue_validated_orphaned_draft_permit_write(
    transaction,
    permit_ref,
    source_permit: Dict[str, Any],
    orphan_state_patch: Dict[str, Any],
    settlement_state_patch: Dict[str, Any],
) -> None:
    """Enqueue only the two already-validated orphan history transitions."""
    if (
        not isinstance(orphan_state_patch, dict)
        or not isinstance(settlement_state_patch, dict)
        or not isinstance(orphan_state_patch.get("stateHistory"), list)
        or not isinstance(settlement_state_patch.get("stateHistory"), list)
        or len(settlement_state_patch["stateHistory"])
        != len(orphan_state_patch["stateHistory"]) + 1
        or settlement_state_patch["stateHistory"][:-1]
        != orphan_state_patch["stateHistory"]
        or settlement_state_patch.get("stateRevision")
        != orphan_state_patch.get("stateRevision") + 1
        or settlement_state_patch["stateHistory"][-1].get("priorHeadHash")
        != orphan_state_patch.get("stateHeadHash")
    ):
        raise GraphSendPermitBlocked(
            "orphaned draft settlement patches are not two linked transitions"
        )
    _validate_permit({**source_permit, **orphan_state_patch})
    _validate_permit({
        **source_permit,
        **orphan_state_patch,
        **settlement_state_patch,
    })
    transaction.update(
        permit_ref,
        {**orphan_state_patch, **settlement_state_patch},
    )


def begin_graph_draft_creation(
    capability: GraphSendCapability,
    source_graph_message_id: str,
    *,
    planned_attachment_count: int = 0,
) -> float:
    """Consume the one-use createReplyAll operation before its POST."""
    if (
        type(planned_attachment_count) is not int
        or planned_attachment_count < 0
        or planned_attachment_count > GRAPH_DRAFT_ATTACHMENT_LIMIT
        or _required_graph_send_history_entries(planned_attachment_count)
        > _PERMIT_STATE_HISTORY_LIMIT
    ):
        raise GraphSendPermitBlocked(
            "Graph draft attachment plan exceeds the retained history bound"
        )
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=True,
    )
    preparation = permit.get("draftPreparation")
    if permit.get("status") != "issued" or preparation is not None:
        state = (preparation or {}).get("state")
        raise GraphSendPermitBlocked(
            "Graph draft creation is one-use; current state is "
            f"{state or permit.get('status')}"
        )
    source_id = str(source_graph_message_id or "").strip()
    if source_id != permit.get("sourceGraphMessageId"):
        raise GraphSendPermitBlocked(
            "Graph draft creation source message drifted from permit"
        )
    timeout = _remaining_provider_seconds(permit, now)
    history = permit.get("stateHistory") or []
    future_entries = (
        _required_graph_send_history_entries(planned_attachment_count) - 1
    )
    if len(history) + future_entries > _PERMIT_STATE_HISTORY_LIMIT:
        raise GraphSendPermitBlocked(
            "Graph send permit has insufficient retained transition history"
        )
    request = {
        "version": GRAPH_DRAFT_PREPARATION_VERSION,
        "state": "create_request_started",
        "sourceGraphMessageId": source_id,
        "createRequestStartedAt": now,
        "plannedAttachmentCount": planned_attachment_count,
    }
    request["createRequestHash"] = _hash(request)
    permit_patch = {
        "draftPreparation": request,
        "updatedAt": SERVER_TIMESTAMP,
    }
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event="draft_create_requested",
        now=now,
    )
    _target, recovered_timeout = _commit_exact_local_transition(
        capability,
        transaction,
        permit,
        state_patch,
        operation="Graph draft-create request",
        timeout_seconds=timeout,
    )
    return recovered_timeout


def complete_graph_draft_creation(
    capability: GraphSendCapability,
    *,
    outcome: str,
    draft_id: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if outcome not in {
        "created",
        "definitely_not_created",
        "needs_reconciliation",
    }:
        raise ValueError(f"unsupported Graph draft-create outcome: {outcome}")
    normalized_draft_id = str(draft_id or "").strip() or None
    if outcome == "created":
        if not normalized_draft_id:
            raise GraphSendPermitBlocked(
                "Graph draft-create success is missing a draft id"
            )
        _validate_successful_draft_operation_evidence(
            "create",
            evidence,
            draft_id=normalized_draft_id,
        )
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=False,
    )
    preparation = dict(permit.get("draftPreparation") or {})
    evidence_payload = dict(evidence or {})
    evidence_hash = _hash(evidence_payload)
    result_state = {
        "created": "draft_created",
        "definitely_not_created": "create_definitely_not_created",
        "needs_reconciliation": "draft_mutation_needs_reconciliation",
    }[outcome]
    if preparation.get("state") == result_state:
        if (
            preparation.get("createOutcomeEvidenceHash") == evidence_hash
            and preparation.get("draftId") == (
                str(draft_id or "").strip() or None
            )
        ):
            return permit
        raise GraphSendPermitBlocked(
            "Graph draft-create outcome was already recorded with different evidence"
        )
    if permit.get("status") != "issued" or preparation.get("state") != "create_request_started":
        raise GraphSendPermitBlocked(
            "Graph draft-create completion has no matching one-use request"
        )
    updated_preparation = {
        **preparation,
        "state": result_state,
        "draftId": normalized_draft_id,
        "createOutcome": outcome,
        "createOutcomeAt": now,
        "createOutcomeEvidence": evidence_payload,
        "createOutcomeEvidenceHash": evidence_hash,
    }
    permit_patch: Dict[str, Any] = {
        "draftPreparation": updated_preparation,
        "updatedAt": SERVER_TIMESTAMP,
    }
    if outcome != "created":
        permit_patch.update({
            "status": (
                "definitely_not_sent"
                if outcome == "definitely_not_created"
                else "needs_reconciliation"
            ),
            "resolvedAt": now,
            "resolutionEvidence": evidence_payload,
            "resolutionEvidenceHash": evidence_hash,
        })
    transaction.update(
        capability.permit_ref,
        _stateful_permit_patch(
            permit,
            permit_patch,
            event=f"draft_create_{outcome}",
            now=now,
        ),
    )
    transaction.commit()
    return {**permit, **permit_patch}


def begin_graph_draft_patch(
    capability: GraphSendCapability,
    *,
    source_graph_message_id: str,
    draft_id: str,
    subject: str,
    html_body: str,
    to_recipients,
    cc_recipients,
    attachments,
) -> Dict[str, Any]:
    """Freeze the later-bound prepared envelope before the one-use PATCH."""
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=True,
    )
    preparation = dict(permit.get("draftPreparation") or {})
    if (
        permit.get("status") != "issued"
        or preparation.get("state") != "draft_created"
    ):
        raise GraphSendPermitBlocked(
            "Graph draft patch is one-use and draft creation is not settled"
        )
    attachment_count = validate_graph_draft_attachment_plan(attachments)
    if attachment_count != preparation.get("plannedAttachmentCount"):
        raise GraphSendPermitBlocked(
            "Graph draft attachment plan drifted from the pre-create plan"
        )
    envelope = _prepared_envelope(
        capability,
        source_graph_message_id=source_graph_message_id,
        draft_id=draft_id,
        subject=subject,
        html_body=html_body,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
        attachments=attachments,
    )
    if (
        envelope.get("sourceGraphMessageId") != permit.get("sourceGraphMessageId")
        or envelope.get("draftId") != preparation.get("draftId")
    ):
        raise GraphSendPermitBlocked(
            "Graph draft prepared envelope drifted from created draft/source"
        )
    timeout = _remaining_provider_seconds(permit, now)
    permit_patch = {
        "draftPreparation": {
            **preparation,
            "state": "patch_request_started",
            "patchRequestStartedAt": now,
        },
        "preparedEnvelope": envelope,
        "updatedAt": SERVER_TIMESTAMP,
    }
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event="draft_patch_requested",
        now=now,
    )
    _target, recovered_timeout = _commit_exact_local_transition(
        capability,
        transaction,
        permit,
        state_patch,
        operation="Graph draft-patch request",
        timeout_seconds=timeout,
    )
    return {
        "preparedEnvelopeHash": envelope["preparedEnvelopeHash"],
        "subject": envelope["subject"],
        "timeoutSeconds": recovered_timeout,
    }


def complete_graph_draft_patch(
    capability: GraphSendCapability,
    *,
    prepared_envelope_hash: str,
    outcome: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if outcome not in {"applied", "needs_reconciliation"}:
        raise ValueError(f"unsupported Graph draft-patch outcome: {outcome}")
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=False,
    )
    preparation = dict(permit.get("draftPreparation") or {})
    envelope = dict(permit.get("preparedEnvelope") or {})
    if outcome == "applied":
        _validate_successful_draft_operation_evidence(
            "patch",
            evidence,
            draft_id=str(envelope.get("draftId") or "").strip(),
            prepared_envelope_hash=prepared_envelope_hash,
        )
    evidence_payload = dict(evidence or {})
    evidence_hash = _hash(evidence_payload)
    result_state = (
        "patch_applied" if outcome == "applied"
        else "draft_mutation_needs_reconciliation"
    )
    if preparation.get("state") == result_state:
        if preparation.get("patchOutcomeEvidenceHash") == evidence_hash:
            return permit
        raise GraphSendPermitBlocked(
            "Graph draft-patch outcome was already recorded with different evidence"
        )
    if (
        permit.get("status") != "issued"
        or preparation.get("state") != "patch_request_started"
        or envelope.get("preparedEnvelopeHash") != prepared_envelope_hash
    ):
        raise GraphSendPermitBlocked(
            "Graph draft-patch completion has no matching one-use request"
        )
    updated_preparation = {
        **preparation,
        "state": result_state,
        "patchOutcome": outcome,
        "patchOutcomeAt": now,
        "patchOutcomeEvidence": evidence_payload,
        "patchOutcomeEvidenceHash": evidence_hash,
    }
    permit_patch: Dict[str, Any] = {
        "draftPreparation": updated_preparation,
        "updatedAt": SERVER_TIMESTAMP,
    }
    if outcome == "needs_reconciliation":
        permit_patch.update({
            "status": "needs_reconciliation",
            "resolvedAt": now,
            "resolutionEvidence": evidence_payload,
            "resolutionEvidenceHash": evidence_hash,
        })
    transaction.update(
        capability.permit_ref,
        _stateful_permit_patch(
            permit,
            permit_patch,
            event=f"draft_patch_{outcome}",
            now=now,
        ),
    )
    transaction.commit()
    return {**permit, **permit_patch}


def begin_graph_draft_attachment(
    capability: GraphSendCapability,
    *,
    prepared_envelope_hash: str,
    attachment_index: int,
    attachment: Dict[str, Any],
) -> Dict[str, Any]:
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=True,
    )
    preparation = dict(permit.get("draftPreparation") or {})
    envelope = dict(permit.get("preparedEnvelope") or {})
    plan = list(envelope.get("attachments") or [])
    outcomes = list(preparation.get("attachmentOutcomes") or [])
    if (
        permit.get("status") != "issued"
        or preparation.get("state") not in {"patch_applied", "attachment_applied"}
        or envelope.get("preparedEnvelopeHash") != prepared_envelope_hash
        or attachment_index != len(outcomes)
        or attachment_index >= len(plan)
        or _attachment_projection(attachment, attachment_index)["attachmentHash"]
        != plan[attachment_index].get("attachmentHash")
    ):
        raise GraphSendPermitBlocked(
            "Graph draft attachment operation drifted or was already consumed"
        )
    timeout = _remaining_provider_seconds(permit, now)
    active = {
        "index": attachment_index,
        "attachmentHash": plan[attachment_index]["attachmentHash"],
        "requestStartedAt": now,
    }
    active["requestHash"] = _hash(active)
    permit_patch = {
        "draftPreparation": {
            **preparation,
            "state": "attachment_request_started",
            "activeAttachment": active,
        },
        "updatedAt": SERVER_TIMESTAMP,
    }
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event=f"draft_attachment_{attachment_index}_requested",
        now=now,
    )
    _target, recovered_timeout = _commit_exact_local_transition(
        capability,
        transaction,
        permit,
        state_patch,
        operation=f"Graph draft attachment {attachment_index} request",
        timeout_seconds=timeout,
    )
    return {
        "timeoutSeconds": recovered_timeout,
        "draftId": envelope.get("draftId"),
        "attachmentIndex": attachment_index,
        "attachmentHash": plan[attachment_index]["attachmentHash"],
    }


def complete_graph_draft_attachment(
    capability: GraphSendCapability,
    *,
    prepared_envelope_hash: str,
    attachment_index: int,
    outcome: str,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if outcome not in {"applied", "needs_reconciliation"}:
        raise ValueError(f"unsupported Graph attachment outcome: {outcome}")
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=False,
    )
    preparation = dict(permit.get("draftPreparation") or {})
    envelope = dict(permit.get("preparedEnvelope") or {})
    active = dict(preparation.get("activeAttachment") or {})
    plan = list(envelope.get("attachments") or [])
    if outcome == "applied":
        expected_attachment_hash = (
            plan[attachment_index].get("attachmentHash")
            if type(attachment_index) is int
            and 0 <= attachment_index < len(plan)
            else None
        )
        _validate_successful_draft_operation_evidence(
            "attachment",
            evidence,
            draft_id=str(envelope.get("draftId") or "").strip(),
            attachment_index=attachment_index,
            attachment_hash=expected_attachment_hash,
        )
    evidence_payload = dict(evidence or {})
    evidence_hash = _hash(evidence_payload)
    outcomes = list(preparation.get("attachmentOutcomes") or [])
    if attachment_index < len(outcomes):
        recorded = outcomes[attachment_index]
        if (
            outcome == "applied"
            and recorded.get("index") == attachment_index
            and recorded.get("outcome") == "applied"
            and recorded.get("evidenceHash") == evidence_hash
        ):
            return permit
        raise GraphSendPermitBlocked(
            "Graph attachment outcome was already recorded with different evidence"
        )
    if preparation.get("state") == "draft_mutation_needs_reconciliation":
        if (
            outcome == "needs_reconciliation"
            and active.get("index") == attachment_index
            and preparation.get("attachmentOutcomeEvidenceHash") == evidence_hash
        ):
            return permit
        raise GraphSendPermitBlocked(
            "Graph attachment reconciliation outcome has different evidence"
        )
    if (
        permit.get("status") != "issued"
        or preparation.get("state") != "attachment_request_started"
        or envelope.get("preparedEnvelopeHash") != prepared_envelope_hash
        or active.get("index") != attachment_index
    ):
        raise GraphSendPermitBlocked(
            "Graph attachment completion has no matching one-use request"
        )
    if outcome == "needs_reconciliation":
        permit_patch = {
            "status": "needs_reconciliation",
            "draftPreparation": {
                **preparation,
                "state": "draft_mutation_needs_reconciliation",
                "attachmentOutcomeEvidence": evidence_payload,
                "attachmentOutcomeEvidenceHash": evidence_hash,
            },
            "resolvedAt": now,
            "resolutionEvidence": evidence_payload,
            "resolutionEvidenceHash": evidence_hash,
            "updatedAt": SERVER_TIMESTAMP,
        }
        transaction.update(
            capability.permit_ref,
            _stateful_permit_patch(
                permit,
                permit_patch,
                event=f"draft_attachment_{attachment_index}_needs_reconciliation",
                now=now,
            ),
        )
        transaction.commit()
        return permit
    outcomes.append({
        "index": attachment_index,
        "attachmentHash": active.get("attachmentHash"),
        "outcome": "applied",
        "outcomeAt": now,
        "evidence": evidence_payload,
        "evidenceHash": evidence_hash,
    })
    permit_patch = {
        "draftPreparation": {
            **preparation,
            "state": "attachment_applied",
            "activeAttachment": None,
            "attachmentOutcomes": outcomes,
        },
        "updatedAt": SERVER_TIMESTAMP,
    }
    transaction.update(
        capability.permit_ref,
        _stateful_permit_patch(
            permit,
            permit_patch,
            event=f"draft_attachment_{attachment_index}_applied",
            now=now,
        ),
    )
    transaction.commit()
    return permit


def finalize_graph_draft_preparation(
    capability: GraphSendCapability,
    *,
    prepared_envelope_hash: str,
) -> Dict[str, Any]:
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=True,
    )
    preparation = dict(permit.get("draftPreparation") or {})
    envelope = dict(permit.get("preparedEnvelope") or {})
    if preparation.get("state") == "prepared":
        if envelope.get("preparedEnvelopeHash") == prepared_envelope_hash:
            return envelope
        raise GraphSendPermitBlocked(
            "Graph prepared envelope was already frozen with another hash"
        )
    plan = list(envelope.get("attachments") or [])
    outcomes = list(preparation.get("attachmentOutcomes") or [])
    if (
        permit.get("status") != "issued"
        or preparation.get("state") not in {"patch_applied", "attachment_applied"}
        or envelope.get("preparedEnvelopeHash") != prepared_envelope_hash
        or len(outcomes) != len(plan)
        or any(
            outcome.get("index") != index
            or outcome.get("attachmentHash") != plan[index].get("attachmentHash")
            or outcome.get("outcome") != "applied"
            for index, outcome in enumerate(outcomes)
        )
    ):
        raise GraphSendPermitBlocked(
            "Graph draft cannot become prepared before every one-use operation settles"
        )
    permit_patch = {
        "draftPreparation": {
            **preparation,
            "state": "prepared",
            "preparedAt": now,
        },
        "updatedAt": SERVER_TIMESTAMP,
    }
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event="draft_prepared",
        now=now,
    )
    target, _timeout = _commit_exact_local_transition(
        capability,
        transaction,
        permit,
        state_patch,
        operation="Graph draft preparation finalize",
    )
    return dict(target.get("preparedEnvelope") or {})


def consume_graph_send_capability(
    capability: GraphSendCapability,
    *,
    source_graph_message_id: str,
    draft_id: str,
    subject: str,
    html_body: str,
    to_recipients,
    cc_recipients,
    attachments,
) -> float:
    """Commit the one-use request_started transition immediately before /send."""
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    if permit.get("status") != "issued":
        raise GraphSendPermitBlocked(
            f"Graph send one-use permit is {permit.get('status')}; expected issued"
        )
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        require_active_lease=True,
    )
    preparation = dict(permit.get("draftPreparation") or {})
    persisted_envelope = dict(permit.get("preparedEnvelope") or {})
    actual_envelope = _prepared_envelope(
        capability,
        source_graph_message_id=source_graph_message_id,
        draft_id=draft_id,
        subject=subject,
        html_body=html_body,
        to_recipients=to_recipients,
        cc_recipients=cc_recipients,
        attachments=attachments,
    )
    if (
        preparation.get("state") != "prepared"
        or persisted_envelope.get("preparedEnvelopeHash")
        != actual_envelope.get("preparedEnvelopeHash")
        or persisted_envelope != actual_envelope
    ):
        raise GraphSendPermitBlocked(
            "Graph send prepared envelope drifted before request_started"
        )
    remaining = _remaining_provider_seconds(permit, now)
    permit_patch = {
        "status": "request_started",
        "requestStartedAt": now,
        "sendPreparedEnvelopeHash": actual_envelope["preparedEnvelopeHash"],
        "capabilityConsumedAt": now,
        "providerTimeoutSeconds": remaining,
        "updatedAt": SERVER_TIMESTAMP,
    }
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event="send_request_started",
        now=now,
    )
    _target, recovered_timeout = _commit_exact_local_transition(
        capability,
        transaction,
        permit,
        state_patch,
        operation="Graph send request",
        timeout_seconds=remaining,
    )
    return recovered_timeout


def _canonical_ref_path(document_ref: Any) -> Optional[str]:
    path = getattr(document_ref, "path", None)
    if not isinstance(path, str):
        return None
    normalized = path.strip("/")
    return normalized or None


def _thread_user_root_path(thread_ref: Any) -> Optional[str]:
    thread_path = _canonical_ref_path(thread_ref)
    if thread_path is None:
        return None
    marker = "/threads/"
    if marker not in thread_path:
        raise GraphSendPermitBlocked(
            "Graph send thread reference has no canonical user path"
        )
    user_path, thread_document_id = thread_path.rsplit(marker, 1)
    if (
        not user_path
        or not thread_document_id
        or "/" in thread_document_id
        or thread_document_id
        != str(getattr(thread_ref, "id", None) or "")
    ):
        raise GraphSendPermitBlocked(
            "Graph send thread reference has a malformed canonical path"
        )
    return user_path


def _require_canonical_ref_path(
    document_ref: Any,
    expected_path: Optional[str],
    *,
    label: str,
) -> None:
    # Real Firestore references always expose ``path``.  A missing thread path
    # is tolerated only by the deliberately tiny in-memory unit doubles.
    if expected_path is None:
        return
    actual_path = _canonical_ref_path(document_ref)
    if actual_path != expected_path.strip("/"):
        raise GraphSendPermitBlocked(
            f"{label} does not use the exact canonical document path"
        )


def _pending_response_path(thread_ref: Any, pending_document_id: str) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    return f"{user_path}/pendingResponses/{pending_document_id}"


def _pending_completion_path(
    thread_ref: Any,
    obligation_id: str,
) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    return (
        f"{user_path}/{PENDING_COMPLETION_OBLIGATION_COLLECTION}/"
        f"{obligation_id}"
    )


def _thread_user_id(thread_ref: Any) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    parts = user_path.split("/")
    if len(parts) < 2 or parts[-2] != "users" or not parts[-1]:
        raise GraphSendPermitBlocked(
            "Graph send thread reference has a malformed user identity"
        )
    return parts[-1]


def _pending_review_path(thread_ref: Any, permit_id: str) -> Optional[str]:
    thread_path = _canonical_ref_path(thread_ref)
    if thread_path is None:
        return None
    # Validate the thread path before using it as an authority root.
    _thread_user_root_path(thread_ref)
    return f"{thread_path}/graphSendReviews/pending-{permit_id}"


def _pending_draft_review_path(
    thread_ref: Any,
    permit_id: str,
) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    return f"{user_path}/graphSendDraftReviews/pending-{permit_id}"


def _operator_audit_path(
    thread_ref: Any,
    settlement_id: str,
) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    return f"{user_path}/graphSendOperatorSettlements/{settlement_id}"


def _draft_review_operator_audit_path(
    thread_ref: Any,
    settlement_id: str,
) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    return (
        f"{user_path}/graphSendDraftReviewSettlements/{settlement_id}"
    )


def _terminal_review_path(
    thread_ref: Any,
    saga: Dict[str, Any],
    permit: Dict[str, Any],
    *,
    kind: str,
) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    identity = hashlib.sha256(
        f"{saga.get('sagaKey')}:{permit.get('permitId')}:{kind}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"{user_path}/terminalGraphSendReviews/terminal-reply-{identity}"


def _pending_dead_letter_path(
    thread_ref: Any,
    pending_ref: Any,
    permit: Dict[str, Any],
) -> Optional[str]:
    user_path = _thread_user_root_path(thread_ref)
    if user_path is None:
        return None
    pending_document_id = str(getattr(pending_ref, "id", None) or "")
    identity = hashlib.sha256(
        (
            f"{pending_document_id}:graph_permit_{permit.get('permitId')}:"
            f"{permit.get('envelopeHash')}"
        ).encode("utf-8")
    ).hexdigest()
    return f"{user_path}/deadLetterQueue/pending-exit-{identity}"


def _validate_draft_review_evidence(
    permit: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    expected_source: str,
) -> str:
    """Validate one authoritative retained-draft review before atomic write."""
    preparation = dict(permit.get("draftPreparation") or {})
    resolution_evidence = dict(permit.get("resolutionEvidence") or {})
    envelope = dict(permit.get("preparedEnvelope") or {})
    draft_id = str(preparation.get("draftId") or "").strip() or None
    prepared_envelope_hash = str(
        envelope.get("preparedEnvelopeHash") or ""
    ).strip() or None
    source_graph_message_id = str(
        payload.get("sourceGraphMessageId") or payload.get("msgId") or ""
    ).strip()
    if (
        permit.get("status")
        not in {
            "needs_reconciliation",
            "settled_draft_needs_review",
            "settled_draft_review_resolved",
        }
        or not _exact_pre_send_draft_resolution(permit)
        or preparation.get("state")
        not in (
            _RETAINABLE_PRE_SEND_DRAFT_STATES
            | {"draft_mutation_needs_reconciliation"}
        )
        or payload.get("status") != "manual_review"
        or payload.get("source") != expected_source
        or payload.get("authoritative") is not True
        or payload.get("alreadySent") is not False
        or payload.get("providerSendStarted") is not False
        or payload.get("sendOutcomeUnknown") is not False
        or payload.get("retryAllowed") is not False
        or payload.get("automaticDeleteAttempted") is not False
        or payload.get("graphSendPermitId") != permit.get("permitId")
        or payload.get("graphSendPermitHash") != permit.get("immutableHash")
        or (str(payload.get("draftId") or "").strip() or None) != draft_id
        or payload.get("draftMutationState") != preparation.get("state")
        or payload.get("draftResolutionEvidenceHash")
        != permit.get("resolutionEvidenceHash")
        or payload.get("failureReason") != resolution_evidence.get("reason")
        or source_graph_message_id != permit.get("sourceGraphMessageId")
        or payload.get("preparedEnvelopeHash") != prepared_envelope_hash
    ):
        raise GraphSendPermitBlocked(
            "retained Graph draft review evidence is not exact pre-send work"
        )
    return _stable_evidence_hash(payload)


_DRAFT_REVIEW_RESOLUTION_ONLY_FIELDS = {
    "resolution",
    "originalReviewEvidenceHash",
    "operatorSettlementAuditRef",
    "operatorSettlementId",
    "resolvedBy",
    "operatorReason",
    "resolvedAt",
}


def _unresolved_draft_review_payload(
    resolved_payload: Dict[str, Any],
) -> Dict[str, Any]:
    original = {
        key: value
        for key, value in dict(resolved_payload or {}).items()
        if key not in _DRAFT_REVIEW_RESOLUTION_ONLY_FIELDS
    }
    original.update({
        "status": "manual_review",
        "retryAllowed": False,
    })
    return original


def _validate_resolved_draft_review_evidence(
    permit: Dict[str, Any],
    payload: Dict[str, Any],
    *,
    expected_source: str,
) -> str:
    original_hash = str(
        payload.get("originalReviewEvidenceHash") or ""
    ).strip()
    settlement_id = str(payload.get("operatorSettlementId") or "").strip()
    operator_id = str(payload.get("resolvedBy") or "").strip()
    operator_reason = str(payload.get("operatorReason") or "").strip()
    resolved_at = _utc(payload.get("resolvedAt"))
    if (
        payload.get("status") != "resolved_not_actionable"
        or payload.get("resolution") != "retained_draft_not_actionable"
        or payload.get("retryAllowed") is not True
        or payload.get("automaticDeleteAttempted") is not False
        or payload.get("providerSendStarted") is not False
        or payload.get("operatorSettlementAuditRef") is None
        or not original_hash
        or not settlement_id
        or not operator_id
        or not operator_reason
        or resolved_at is None
        or original_hash
        != permit.get("operatorOriginalReconciliationEvidenceHash")
    ):
        raise GraphSendPermitBlocked(
            "resolved retained Graph draft review evidence is malformed"
        )
    original_payload = _unresolved_draft_review_payload(payload)
    if _validate_draft_review_evidence(
        permit,
        original_payload,
        expected_source=expected_source,
    ) != original_hash:
        raise GraphSendPermitBlocked(
            "resolved retained Graph draft review original hash drifted"
        )
    resolved_hash = _stable_evidence_hash(payload)
    if (
        resolved_hash != permit.get("draftReviewEvidenceHash")
        or resolved_hash
        != permit.get("operatorResolvedReviewEvidenceHash")
    ):
        raise GraphSendPermitBlocked(
            "resolved retained Graph draft review hash drifted"
        )
    return resolved_hash


def _validate_settled_draft_review_document(
    permit: Dict[str, Any],
    thread_ref: Any,
    *,
    transaction=None,
) -> None:
    """Dereference and validate the authoritative settled draft review."""
    if permit.get("status") not in {
        "settled_draft_needs_review",
        "settled_draft_review_resolved",
    }:
        return
    review_ref = permit.get("draftReviewEvidenceRef")
    if review_ref is None:
        raise GraphSendPermitBlocked(
            "settled Graph draft review reference is missing"
        )
    if permit.get("issuerKind") == "pending_response":
        _require_canonical_ref_path(
            review_ref,
            _pending_draft_review_path(
                thread_ref,
                str(permit.get("permitId") or ""),
            ),
            label="settled pending draft review reference",
        )
        expected_source = "pendingGraphSendProtocol"
    elif permit.get("issuerKind") == "terminal_saga":
        user_path = _thread_user_root_path(thread_ref)
        if user_path is not None:
            actual_path = _canonical_ref_path(review_ref)
            prefix = f"{user_path}/terminalGraphSendReviews/"
            document_id = (
                actual_path[len(prefix):]
                if isinstance(actual_path, str)
                and actual_path.startswith(prefix)
                else ""
            )
            if not document_id or "/" in document_id:
                raise GraphSendPermitBlocked(
                    "settled terminal draft review reference left its exact user collection"
                )
        expected_source = "terminalGraphSendProtocol"
    else:
        raise GraphSendPermitBlocked(
            "settled Graph draft review has an unsupported issuer"
        )

    review_snapshot = (
        review_ref.get(transaction=transaction)
        if transaction is not None
        else review_ref.get()
    )
    if review_snapshot.exists is not True:
        raise GraphSendPermitBlocked(
            "settled Graph draft review document is missing"
        )
    review_payload = review_snapshot.to_dict() or {}
    if permit.get("status") == "settled_draft_needs_review":
        review_hash = _validate_draft_review_evidence(
            permit,
            review_payload,
            expected_source=expected_source,
        )
    else:
        review_hash = _validate_resolved_draft_review_evidence(
            permit,
            review_payload,
            expected_source=expected_source,
        )
    if review_hash != permit.get("draftReviewEvidenceHash"):
        raise GraphSendPermitBlocked(
            "settled Graph draft review document hash drifted"
        )
    if permit.get("status") != "settled_draft_review_resolved":
        return

    audit_ref = permit.get("operatorSettlementAuditRef")
    settlement_id = str(
        review_payload.get("operatorSettlementId") or ""
    ).strip()
    if (
        audit_ref is None
        or not _same_document_ref(
            review_payload.get("operatorSettlementAuditRef"),
            audit_ref,
        )
    ):
        raise GraphSendPermitBlocked(
            "resolved retained Graph draft review audit reference drifted"
        )
    _require_canonical_ref_path(
        audit_ref,
        _draft_review_operator_audit_path(thread_ref, settlement_id),
        label="resolved draft review operator audit reference",
    )
    audit_snapshot = (
        audit_ref.get(transaction=transaction)
        if transaction is not None
        else audit_ref.get()
    )
    if audit_snapshot.exists is not True:
        raise GraphSendPermitBlocked(
            "resolved retained Graph draft review audit is missing"
        )
    audit = audit_snapshot.to_dict() or {}
    if (
        _stable_evidence_hash(audit)
        != permit.get("operatorSettlementAuditHash")
        or audit.get("version") != 1
        or audit.get("settlementId") != settlement_id
        or audit.get("action")
        != "confirm_retained_draft_not_actionable"
        or audit.get("operatorId") != review_payload.get("resolvedBy")
        or audit.get("operatorReason")
        != review_payload.get("operatorReason")
        or audit.get("threadId") != permit.get("threadId")
        or audit.get("clientId") != permit.get("clientId")
        or audit.get("pendingDocumentId")
        != permit.get("issuerDocumentId")
        or audit.get("graphSendPermitId") != permit.get("permitId")
        or audit.get("graphSendPermitHash") != permit.get("immutableHash")
        or audit.get("reviewEvidenceHash")
        != permit.get("operatorOriginalReconciliationEvidenceHash")
        or not _same_document_ref(audit.get("reviewEvidenceRef"), review_ref)
        or audit.get("resolution") != "retained_draft_not_actionable"
        or audit.get("providerSendStarted") is not False
        or audit.get("automaticDeleteAttempted") is not False
        or audit.get("retryAllowed") is not True
        or _utc(audit.get("resolvedAt"))
        != _utc(review_payload.get("resolvedAt"))
    ):
        raise GraphSendPermitBlocked(
            "resolved retained Graph draft review audit linkage drifted"
        )


def _require_permit_issuer_binding(
    thread_ref: Any,
    permit: Dict[str, Any],
) -> None:
    issuer_document_id = str(
        permit.get("issuerDocumentId") or ""
    ).strip()
    if permit.get("issuerKind") == "pending_response":
        expected_path = _pending_response_path(
            thread_ref,
            issuer_document_id,
        )
    else:
        user_path = _thread_user_root_path(thread_ref)
        expected_path = (
            f"{user_path}/threads/{issuer_document_id}"
            if user_path is not None
            else None
        )
    if (
        expected_path is not None
        and permit.get("issuerDocumentPath") != expected_path
    ):
        raise GraphSendPermitBlocked(
            "Graph send permit issuer path is outside the thread's canonical user root"
        )


def _require_fresh_sent_lookup(
    lookup_completed_at: Optional[datetime],
    *,
    now: datetime,
    pending_data: Optional[Dict[str, Any]] = None,
) -> None:
    last_checked_at = _utc(
        (pending_data or {}).get("graphSendSentLastCheckedAt")
    )
    if (
        lookup_completed_at is None
        or lookup_completed_at < now - timedelta(minutes=5)
        or lookup_completed_at > now + timedelta(seconds=5)
        or (
            last_checked_at is not None
            and lookup_completed_at < last_checked_at
        )
    ):
        raise GraphSendPermitBlocked(
            "operator settlement requires a fresh bounded Sent Items lookup"
        )


def resolve_graph_send_permit(
    capability: GraphSendCapability,
    status: str,
    *,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    legal_transitions = {
        "issued": {"request_started", "definitely_not_sent", "needs_reconciliation"},
        "request_started": {"accepted", "needs_reconciliation"},
        "needs_reconciliation": {"reconciled_sent"},
    }
    if status not in {
        "accepted",
        "definitely_not_sent",
        "needs_reconciliation",
        "reconciled_sent",
    }:
        raise ValueError(f"unsupported Graph send permit resolution: {status}")
    transaction = capability.firestore_client.transaction()
    now = datetime.now(timezone.utc)
    permit = _read_exact_capability_permit(transaction, capability)
    _validate_capability_issuer(
        transaction,
        capability,
        now=now,
        # Provider responses may arrive after a lease expires, but never after
        # the durable issuer owner/fence or exact pending claim has changed.
        require_active_lease=False,
    )
    current_status = permit.get("status")
    evidence_payload = dict(evidence or {})
    evidence_hash = _hash(evidence_payload)
    if current_status == status:
        if permit.get("resolutionEvidenceHash") == evidence_hash:
            return permit
        raise GraphSendPermitBlocked(
            "Graph send permit outcome already has different evidence"
        )
    if current_status in GRAPH_SEND_RESOLVED_STATUSES:
        if current_status == status:
            return permit
        raise GraphSendPermitBlocked(
            "Graph send permit already has a different resolved outcome"
        )
    if status not in legal_transitions.get(current_status, set()):
        raise GraphSendPermitBlocked(
            f"illegal Graph send permit transition: {current_status} -> {status}"
        )
    updated = {
        **permit,
        "status": status,
        "resolvedAt": now,
        "resolutionEvidence": evidence_payload,
        "resolutionEvidenceHash": evidence_hash,
    }
    permit_patch = {
        "status": status,
        "resolvedAt": updated["resolvedAt"],
        "resolutionEvidence": updated["resolutionEvidence"],
        "resolutionEvidenceHash": evidence_hash,
        "updatedAt": SERVER_TIMESTAMP,
    }
    transaction.update(
        capability.permit_ref,
        _stateful_permit_patch(
            permit,
            permit_patch,
            event=f"provider_{status}",
            now=now,
        ),
    )
    transaction.commit()
    return updated


def _validate_terminal_claim_identity(
    claim_data: Dict[str, Any],
    saga: Dict[str, Any],
    issuer_owner: str,
    issuer_fence: int,
    *,
    now: datetime,
    require_active_lease: bool,
) -> Dict[str, Any]:
    claim = (claim_data or {}).get("terminalSagaClaim")
    lease_until = _utc((claim or {}).get("leaseUntil"))
    if (
        not isinstance(claim, dict)
        or claim.get("sagaKey") != saga.get("sagaKey")
        or claim.get("immutableHash") != saga.get("immutableHash")
        or claim.get("owner") != issuer_owner
        or claim.get("fencingToken") != issuer_fence
        or (
            require_active_lease
            and (lease_until is None or lease_until <= now)
        )
    ):
        raise GraphSendPermitBlocked(
            "terminal saga owner/fence changed before reply permit settlement"
        )
    return claim


def _validate_exact_terminal_sent_evidence(
    permit: Dict[str, Any],
    sent_evidence: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    evidence = dict(sent_evidence or {})
    evidence_recipient = str(evidence.get("recipient") or "").strip().lower()
    evidence_conversation = str(evidence.get("conversationId") or "").strip()
    permit_conversation = str(permit.get("conversationId") or "").strip()
    request_started_at = _utc(permit.get("requestStartedAt"))
    sent_at = _utc(evidence.get("sentDateTime"))
    sent_message_id = str(
        evidence.get("sentMessageId") or evidence.get("id") or ""
    ).strip()
    immutable_draft_id = str(
        (permit.get("preparedEnvelope") or {}).get("draftId") or ""
    ).strip()
    prepared_envelope_hash = (
        permit.get("sendPreparedEnvelopeHash")
        or (permit.get("preparedEnvelope") or {}).get("preparedEnvelopeHash")
    )
    prepared_envelope = permit.get("preparedEnvelope") or {}
    actual_to = evidence.get("toRecipients")
    actual_cc = evidence.get("ccRecipients")
    actual_bcc = evidence.get("bccRecipients")
    actual_subject = evidence.get("subject")
    actual_body = evidence.get("body")
    actual_attachments = evidence.get("attachments")
    actual_envelope_keys_present = all(
        key in evidence
        for key in (
            "toRecipients",
            "ccRecipients",
            "bccRecipients",
            "subject",
            "body",
            "attachments",
        )
    )
    try:
        actual_to_addresses = _recipient_addresses(
            actual_to,
            field="actual To",
        )
        actual_cc_addresses = _recipient_addresses(
            actual_cc,
            field="actual Cc",
        )
        actual_bcc_addresses = _recipient_addresses(
            actual_bcc,
            field="actual Bcc",
        )
    except GraphSendPermitBlocked as exc:
        raise GraphSendPermitBlocked(
            "terminal takeover exact Sent recipient evidence is malformed"
        ) from exc
    actual_recipients_well_formed = bool(
        isinstance(actual_to, list)
        and isinstance(actual_cc, list)
        and isinstance(actual_bcc, list)
        and len(actual_to_addresses) == len(actual_to)
        and len(actual_cc_addresses) == len(actual_cc)
        and len(actual_bcc_addresses) == len(actual_bcc)
    )
    try:
        actual_attachment_multiset = _canonical_actual_attachment_multiset(
            actual_attachments,
        )
        prepared_attachment_multiset = _canonical_attachment_multiset_from_plan(
            prepared_envelope.get("attachments"),
        )
    except GraphSendPermitError as exc:
        raise GraphSendPermitBlocked(
            "terminal takeover exact Sent attachment evidence is malformed"
        ) from exc
    if (
        not sent_message_id
        or not immutable_draft_id
        or sent_message_id != immutable_draft_id
        or evidence.get("isDraft") is not False
        or evidence_recipient != permit.get("recipient")
        or evidence.get("bodyHash") != permit.get("bodyHash")
        or evidence.get("permitId") != permit.get("permitId")
        or evidence.get("sourceGraphMessageId")
        != permit.get("sourceGraphMessageId")
        or not prepared_envelope_hash
        or evidence.get("preparedEnvelopeHash") != prepared_envelope_hash
        or not actual_envelope_keys_present
        or not actual_recipients_well_formed
        or actual_to_addresses != prepared_envelope.get("toRecipients")
        or actual_cc_addresses != prepared_envelope.get("ccRecipients")
        or bool(actual_bcc_addresses)
        or not isinstance(actual_subject, str)
        or actual_subject != prepared_envelope.get("subject")
        or actual_attachment_multiset != prepared_attachment_multiset
        or not isinstance(actual_body, dict)
        or canonical_graph_body_hash(actual_body)
        != prepared_envelope.get("htmlBodyHash")
        or request_started_at is None
        or sent_at is None
        or (
            permit_conversation
            and evidence_conversation != permit_conversation
        )
    ):
        raise GraphSendPermitBlocked(
            "terminal takeover requires exact Sent evidence for the retained permit"
        )
    return evidence


def cas_terminal_reply_transition(
    firestore_client,
    thread_ref,
    claim_ref,
    saga: Dict[str, Any],
    issuer_owner: str,
    issuer_fence: int,
    *,
    expected_attempt_status: str,
    thread_patch: Dict[str, Any],
    permit_settlement: str,
    capability: Optional[GraphSendCapability] = None,
    sent_evidence: Optional[Dict[str, Any]] = None,
    pending_upsert=None,
    pending_delete_ref=None,
    side_documents=(),
) -> bool:
    """Settle a terminal permit and its exact reply transition atomically.

    The in-memory capability is required for a direct issuer settlement.  A
    later fenced owner may settle without the plaintext secret only from exact
    retained attempt/pointer evidence: definite-unsent, or a matching Sent
    record for an ambiguous/accepted provider request.  It can never authorize
    another provider send.
    """
    if permit_settlement not in {
        "settled_sent",
        "settled_definitely_not_sent",
        "settled_draft_needs_review",
        "reconciliation_recorded",
    }:
        raise ValueError(
            f"unsupported terminal Graph permit settlement: {permit_settlement}"
        )
    if not isinstance(thread_patch, dict) or not thread_patch:
        raise ValueError("terminal reply CAS requires a non-empty thread patch")
    allowed_thread_patch_fields = {
        "terminalReplyOwed",
        "terminalReplyOutcome",
        "terminalReplyResolvedAt",
        "terminalReplyAttempt",
        "updatedAt",
    }
    unowned_thread_fields = set(thread_patch) - allowed_thread_patch_fields
    if unowned_thread_fields:
        raise GraphSendPermitBlocked(
            "terminal reply thread patch contains unowned fields outside its allowlist"
        )
    side_documents = tuple(side_documents)
    if pending_upsert is not None and pending_delete_ref is not None:
        raise ValueError("terminal reply CAS cannot upsert and delete pending work")
    if pending_upsert is not None and permit_settlement != "settled_definitely_not_sent":
        raise GraphSendPermitBlocked(
            "terminal pending upsert is only valid for a definitely-unsent settlement"
        )
    if pending_delete_ref is not None and permit_settlement != "settled_sent":
        raise GraphSendPermitBlocked(
            "terminal pending delete is only valid for an exact-Sent settlement"
        )
    expected_side_document_count = (
        1
        if permit_settlement
        in {"settled_draft_needs_review", "reconciliation_recorded"}
        else 0
    )
    if len(side_documents) != expected_side_document_count:
        raise GraphSendPermitBlocked(
            "terminal permit settlement has the wrong side-document effects"
        )

    claim_thread_id = str(
        (saga.get("finalizationPlan") or {}).get("claimThreadId") or ""
    ).strip()
    user_path = _thread_user_root_path(thread_ref)
    expected_claim_path = (
        f"{user_path}/threads/{claim_thread_id}"
        if user_path is not None and claim_thread_id
        else None
    )
    if (
        not claim_thread_id
        or str(getattr(claim_ref, "id", None) or "")
        != claim_thread_id
    ):
        raise GraphSendPermitBlocked(
            "terminal permit settlement claim root drifted from the saga"
        )
    _require_canonical_ref_path(
        claim_ref,
        expected_claim_path,
        label="terminal permit settlement claim reference",
    )
    exact_claim_document_id, exact_claim_document_path = (
        _issuer_document_identity(
            claim_ref,
            issuer_kind="terminal_saga",
        )
    )

    transaction = firestore_client.transaction()
    deferred_sets = []
    deferred_deletes = []
    now = datetime.now(timezone.utc)
    thread_patch = dict(thread_patch)
    if thread_patch.get("terminalReplyResolvedAt") is SERVER_TIMESTAMP:
        thread_patch["terminalReplyResolvedAt"] = now
    normalized_attempt = thread_patch.get("terminalReplyAttempt")
    if isinstance(normalized_attempt, dict):
        normalized_attempt = dict(normalized_attempt)
        if normalized_attempt.get("committedAt") is SERVER_TIMESTAMP:
            normalized_attempt["committedAt"] = now
        thread_patch["terminalReplyAttempt"] = normalized_attempt
    same_claim_root = _same_document_ref(claim_ref, thread_ref)
    thread_snapshot = thread_ref.get(transaction=transaction)
    claim_snapshot = (
        thread_snapshot
        if same_claim_root
        else claim_ref.get(transaction=transaction)
    )
    if not thread_snapshot.exists or not claim_snapshot.exists:
        raise GraphSendPermitBlocked("terminal reply settlement roots are missing")
    thread_data = thread_snapshot.to_dict() or {}
    claim_data = claim_snapshot.to_dict() or {}
    _validate_terminal_claim_identity(
        claim_data,
        saga,
        issuer_owner,
        issuer_fence,
        now=now,
        # The exact original issuer may persist a provider response after its
        # lease elapsed, but only while its owner/fence is still current.  A
        # takeover path has no capability and must own a live recovery lease.
        require_active_lease=capability is None,
    )

    attempt = thread_data.get("terminalReplyAttempt")
    if (
        not thread_data.get("terminalReplyOwed")
        or not isinstance(attempt, dict)
        or attempt.get("sagaKey") != saga.get("sagaKey")
        or attempt.get("status") != expected_attempt_status
    ):
        raise GraphSendPermitBlocked(
            "terminal reply attempt changed before permit settlement"
        )

    if capability is not None:
        if (
            capability.issuer_kind != "terminal_saga"
            or capability.issuer_owner != issuer_owner
            or capability.issuer_fence != issuer_fence
            or not _same_document_ref(capability.thread_ref, thread_ref)
            or not _same_document_ref(capability.issuer_ref, claim_ref)
        ):
            raise GraphSendPermitBlocked(
                "terminal reply capability does not belong to the current owner/fence"
            )
        permit = _read_exact_capability_permit(transaction, capability)
        permit_ref = capability.permit_ref
    else:
        permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
        if permit is None or permit_ref is None:
            raise GraphSendPermitBlocked(
                "terminal reply retained permit is missing during takeover"
            )

    if (
        permit.get("issuerKind") != "terminal_saga"
        or permit.get("threadId")
        != str(getattr(thread_ref, "id", None) or "")
        or permit.get("issuerDocumentId")
        != exact_claim_document_id
        or permit.get("issuerDocumentPath")
        != exact_claim_document_path
        or attempt.get("graphSendPermitId") != permit.get("permitId")
        or attempt.get("graphSendPermitHash") != permit.get("immutableHash")
        or permit.get("threadId") != str(getattr(thread_ref, "id", None) or "")
        or permit.get("bodyHash") != attempt.get("responseBodyHash")
        or permit.get("sourceGraphMessageId") != saga.get("sourceGraphMessageId")
        or permit.get("conversationId") != saga.get("sourceConversationId")
        or permit.get("recipient")
        != str(saga.get("replyRecipient") or "").strip().lower()
    ):
        raise GraphSendPermitBlocked(
            "terminal reply attempt drifted from retained Graph permit"
        )

    current_status = permit.get("status")
    pre_settlement_state_patch: Dict[str, Any] = {}
    capabilityless_source_permit: Optional[Dict[str, Any]] = None
    capabilityless_projection = False
    if capability is None and current_status == "issued":
        recovery_kind = expired_graph_send_pre_send_recovery_kind(
            permit,
            now=now,
        )
        projection_outcome = None
        if (
            permit_settlement == "settled_draft_needs_review"
            and recovery_kind == "draft_needs_review"
        ):
            projection_outcome = "draft_needs_review"
        elif (
            permit_settlement == "settled_definitely_not_sent"
            and recovery_kind == "definitely_not_started"
        ):
            projection_outcome = "definitely_not_started"
        if projection_outcome is not None:
            capabilityless_source_permit = permit
            permit, pre_settlement_state_patch, _projection_event = (
                _lost_capability_pre_send_projection_patch(
                    permit,
                    now=now,
                    outcome=projection_outcome,
                )
            )
            current_status = permit.get("status")
            capabilityless_projection = True
    elif (
        capability is None
        and current_status == "definitely_not_sent"
        and permit_settlement == "settled_definitely_not_sent"
        and expired_graph_send_pre_send_recovery_kind(
            permit,
            now=now,
        )
        == "definitely_not_sent"
        and (
            _exact_definitely_not_created_resolution(permit)
            or _exact_preflight_definitely_not_sent_resolution(permit)
        )
    ):
        capabilityless_source_permit = permit
    if permit_settlement == "settled_sent":
        exact_sent_evidence = _validate_exact_terminal_sent_evidence(
            permit,
            sent_evidence,
        )
        if current_status not in {
            "request_started",
            "needs_reconciliation",
            "accepted",
            "reconciled_sent",
        }:
            raise GraphSendPermitBlocked(
                "terminal reply permit has no sent/ambiguous provider outcome"
            )
        permit_patch = {
            "status": "settled_sent",
            "issuerSettledAt": now,
            "terminalSentEvidence": exact_sent_evidence,
            "updatedAt": SERVER_TIMESTAMP,
        }
        if permit.get("terminalSendReviewRequired") is True:
            review_ref = permit.get("terminalSendReviewEvidenceRef")
            review_hash = permit.get("terminalSendReviewEvidenceHash")
            if review_ref is None or not str(review_hash or "").strip():
                raise GraphSendPermitBlocked(
                    "terminal retained send review linkage is malformed"
                )
            _require_canonical_ref_path(
                review_ref,
                _terminal_review_path(
                    thread_ref,
                    saga,
                    permit,
                    kind="send_needs_reconciliation",
                ),
                label="terminal retained review reference",
            )
            review_snapshot = review_ref.get(transaction=transaction)
            review_data = (
                review_snapshot.to_dict() if review_snapshot.exists else {}
            )
            if (
                not review_snapshot.exists
                or _stable_evidence_hash(review_data) != review_hash
                or review_data.get("alreadySent") is not None
                or review_data.get("sendOutcomeUnknown") is not True
                or review_data.get("retryAllowed") is not False
                or review_data.get("graphSendPermitId")
                != permit.get("permitId")
                or review_data.get("graphSendPermitHash")
                != permit.get("immutableHash")
            ):
                raise GraphSendPermitBlocked(
                    "terminal retained send review evidence drifted"
                )
            resolved_review = {
                **review_data,
                "status": "reconciled_sent",
                "alreadySent": True,
                "sendOutcomeUnknown": False,
                "retryAllowed": False,
                "originalReviewEvidenceHash": review_hash,
                "sentMessageId": (
                    exact_sent_evidence.get("sentMessageId")
                    or exact_sent_evidence.get("id")
                ),
                "sentDateTime": exact_sent_evidence.get("sentDateTime"),
                "resolvedAt": now,
                "updatedAt": now,
            }
            resolved_review_hash = _stable_evidence_hash(resolved_review)
            deferred_sets.append((review_ref, resolved_review))
            permit_patch.update({
                "terminalSendReviewRequired": False,
                "terminalResolvedReviewEvidenceHash": resolved_review_hash,
            })
    elif permit_settlement == "settled_definitely_not_sent":
        if current_status != "definitely_not_sent":
            raise GraphSendPermitBlocked(
                "terminal reply permit is not definitely unsent"
            )
        permit_patch = {
            "status": "settled_definitely_not_sent",
            "issuerSettledAt": now,
            "updatedAt": SERVER_TIMESTAMP,
        }
    elif permit_settlement == "settled_draft_needs_review":
        preparation = dict(permit.get("draftPreparation") or {})
        if (
            permit.get("requestStartedAt") is not None
            or current_status != "needs_reconciliation"
            or not _exact_pre_send_draft_resolution(permit)
            or preparation.get("state")
            not in (
                _RETAINABLE_PRE_SEND_DRAFT_STATES
                | {"draft_mutation_needs_reconciliation"}
            )
        ):
            raise GraphSendPermitBlocked(
                "terminal draft review cannot imply a provider send"
            )
        permit_patch = {
            "status": "settled_draft_needs_review",
            "issuerSettledAt": now,
            "draftReviewRequired": True,
            "updatedAt": SERVER_TIMESTAMP,
        }
    else:
        if current_status not in {
            "accepted",
            "request_started",
            "needs_reconciliation",
        }:
            raise GraphSendPermitBlocked(
                "terminal reconciliation record has no ambiguous send request"
            )
        permit_patch = {
            "status": "needs_reconciliation",
            "reconciliationRecordedAt": now,
            "updatedAt": SERVER_TIMESTAMP,
        }

    patched_attempt = thread_patch.get("terminalReplyAttempt")
    if not isinstance(patched_attempt, dict) or (
        patched_attempt.get("sagaKey") != saga.get("sagaKey")
        or patched_attempt.get("responseBodyHash") != permit.get("bodyHash")
        or patched_attempt.get("graphSendPermitId") != permit.get("permitId")
        or patched_attempt.get("graphSendPermitHash") != permit.get("immutableHash")
    ):
        raise GraphSendPermitBlocked(
            "terminal reply outcome patch does not preserve exact permit evidence"
        )
    if thread_patch.get("terminalReplyOwed") is False and (
        not str(thread_patch.get("terminalReplyOutcome") or "").strip()
        or patched_attempt.get("status") not in {"committed", "reconciled"}
    ):
        raise GraphSendPermitBlocked(
            "terminal resolved reply patch is missing a committed outcome"
        )
    if permit_settlement == "reconciliation_recorded" and (
        thread_patch.get("terminalReplyOwed") is False
        or patched_attempt.get("status") != "needs_reconciliation"
    ):
        raise GraphSendPermitBlocked(
            "terminal ambiguous send must remain owed and reconciliation-only"
        )

    effective_owed = thread_patch.get(
        "terminalReplyOwed",
        thread_data.get("terminalReplyOwed"),
    )
    effective_outcome = thread_patch.get(
        "terminalReplyOutcome",
        thread_data.get("terminalReplyOutcome"),
    )
    attempt_status = patched_attempt.get("status")
    attempt_outcome = patched_attempt.get("outcome")
    if permit_settlement == "settled_sent" and (
        effective_owed is not False
        or attempt_status not in {"committed", "reconciled"}
        or effective_outcome
        not in {"sent_indexed", "sent_unindexed", "sent_reconciled"}
        or attempt_outcome != effective_outcome
    ):
        raise GraphSendPermitBlocked(
            "terminal exact-Sent settlement does not map to a committed sent outcome"
        )
    if permit_settlement == "settled_draft_needs_review" and (
        effective_owed is not False
        or effective_outcome != "draft_needs_review"
        or attempt_status != "committed"
        or attempt_outcome != "draft_needs_review"
    ):
        raise GraphSendPermitBlocked(
            "terminal draft-review settlement does not map to its terminal outcome"
        )
    if permit_settlement == "reconciliation_recorded" and (
        effective_owed is not True
        or attempt_status != "needs_reconciliation"
    ):
        raise GraphSendPermitBlocked(
            "terminal reconciliation settlement does not preserve owed work"
        )
    if permit_settlement == "settled_definitely_not_sent":
        if pending_upsert is not None:
            valid_definite_unsent_outcome = (
                effective_owed is True
                and attempt_status == "queueing_response_retry"
            )
        else:
            valid_definite_unsent_outcome = (
                effective_owed is False
                and attempt_status == "committed"
                and isinstance(effective_outcome, str)
                and bool(effective_outcome.strip())
                and attempt_outcome == effective_outcome
                and effective_outcome
                not in {"sent_indexed", "sent_unindexed", "sent_reconciled"}
            )
        if not valid_definite_unsent_outcome:
            raise GraphSendPermitBlocked(
                "terminal definitely-unsent settlement has incompatible thread effects"
            )

    expected_pending = {
        "threadId": str(getattr(thread_ref, "id", None) or ""),
        "msgId": permit.get("sourceGraphMessageId"),
        "recipient": permit.get("recipient"),
        "responseBody": _canonical_terminal_response_body(saga),
        "clientId": permit.get("clientId"),
        "conversationId": permit.get("conversationId"),
    }
    terminal_pending_ref = None
    terminal_pending_source_exists = False
    terminal_pending_source_data: Dict[str, Any] = {}
    terminal_pending_target_data: Optional[Dict[str, Any]] = None
    if pending_upsert is not None:
        pending_ref, pending_payload = pending_upsert
        terminal_pending_ref = pending_ref
        _require_canonical_ref_path(
            pending_ref,
            _pending_response_path(
                thread_ref,
                str(getattr(thread_ref, "id", None) or ""),
            ),
            label="terminal pending upsert reference",
        )
        if pending_envelope_hash(pending_payload) != pending_envelope_hash(
            expected_pending
        ):
            raise GraphSendPermitBlocked(
                "terminal definite-unsent pending payload drifted from permit"
            )
        pending_snapshot = pending_ref.get(transaction=transaction)
        terminal_pending_source_exists = pending_snapshot.exists
        if pending_snapshot.exists:
            pending_data = pending_snapshot.to_dict() or {}
            terminal_pending_source_data = pending_data
            terminal_pending_target_data = pending_data
            if pending_envelope_hash(pending_data) != pending_envelope_hash(
                expected_pending
            ):
                raise GraphSendPermitBlocked(
                    "terminal definite-unsent pending document belongs to another intent"
                )
            pending_owner = pending_data.get("processingBy")
            pending_lease = _utc(pending_data.get("processingLeaseUntil"))
            if pending_owner and (pending_lease is None or pending_lease > now):
                raise GraphSendPermitBlocked(
                    "terminal definite-unsent pending document has an active owner"
                )
        else:
            terminal_pending_target_data = dict(pending_payload)
            deferred_sets.append((pending_ref, terminal_pending_target_data))
    elif pending_delete_ref is not None:
        _require_canonical_ref_path(
            pending_delete_ref,
            _pending_response_path(
                thread_ref,
                str(getattr(thread_ref, "id", None) or ""),
            ),
            label="terminal pending delete reference",
        )
        pending_snapshot = pending_delete_ref.get(transaction=transaction)
        if pending_snapshot.exists:
            pending_data = pending_snapshot.to_dict() or {}
            if pending_envelope_hash(pending_data) != pending_envelope_hash(
                expected_pending
            ):
                raise GraphSendPermitBlocked(
                    "terminal Sent reconciliation pending document drifted"
                )
            pending_owner = pending_data.get("processingBy")
            pending_lease = _utc(pending_data.get("processingLeaseUntil"))
            if pending_owner and (pending_lease is None or pending_lease > now):
                raise GraphSendPermitBlocked(
                    "terminal Sent reconciliation found active pending work"
                )
            deferred_deletes.append(pending_delete_ref)

    if (
        permit_settlement == "settled_draft_needs_review"
        and len(side_documents) != 1
    ):
        raise GraphSendPermitBlocked(
            "terminal retained draft review requires one exact evidence document"
        )
    terminal_review_ref = None
    terminal_review_hash = None
    terminal_review_payload = None
    for document_ref, payload in side_documents:
        evidence_payload = dict(payload or {})
        if capabilityless_projection:
            evidence_payload = _orphaned_draft_review_payload(
                permit,
                evidence_payload,
            )
            orphan_review_snapshot = document_ref.get(
                transaction=transaction
            )
            if orphan_review_snapshot.exists:
                raise GraphSendPermitBlocked(
                    "orphaned terminal draft review id already exists"
                )
        review_kind = (
            "send_needs_reconciliation"
            if permit_settlement == "reconciliation_recorded"
            else "draft_needs_review"
        )
        _require_canonical_ref_path(
            document_ref,
            _terminal_review_path(
                thread_ref,
                saga,
                permit,
                kind=review_kind,
            ),
            label="terminal review reference",
        )
        if (
            evidence_payload.get("graphSendPermitId") != permit.get("permitId")
            or evidence_payload.get("graphSendPermitHash")
            != permit.get("immutableHash")
        ):
            raise GraphSendPermitBlocked(
                "terminal reconciliation evidence drifted from retained permit"
            )
        draft_review_hash = None
        if permit_settlement == "settled_draft_needs_review":
            draft_review_hash = _validate_draft_review_evidence(
                permit,
                evidence_payload,
                expected_source="terminalGraphSendProtocol",
            )
        if permit_settlement == "reconciliation_recorded":
            if (
                len(side_documents) != 1
                or evidence_payload.get("alreadySent") is not None
                or evidence_payload.get("providerSendStarted") is not True
                or evidence_payload.get("sendOutcomeUnknown") is not True
                or evidence_payload.get("retryAllowed") is not False
                or evidence_payload.get("authoritative") is not True
                or evidence_payload.get("source")
                != "terminalGraphSendProtocol"
            ):
                raise GraphSendPermitBlocked(
                    "terminal ambiguous send review evidence is not exact tri-state protocol work"
                )
            terminal_review_ref = document_ref
            terminal_review_hash = _stable_evidence_hash(evidence_payload)
        elif permit_settlement == "settled_draft_needs_review":
            terminal_review_ref = document_ref
            terminal_review_hash = draft_review_hash
            terminal_review_payload = evidence_payload
        deferred_sets.append((document_ref, evidence_payload))

    if permit_settlement == "reconciliation_recorded":
        if terminal_review_ref is None or terminal_review_hash is None:
            raise GraphSendPermitBlocked(
                "terminal ambiguous send requires server-owned review evidence"
            )
        permit_patch.update({
            "terminalSendReviewRequired": True,
            "terminalSendReviewEvidenceRef": terminal_review_ref,
            "terminalSendReviewEvidenceHash": terminal_review_hash,
        })
    elif permit_settlement == "settled_draft_needs_review":
        if terminal_review_ref is None or terminal_review_hash is None:
            raise GraphSendPermitBlocked(
                "terminal retained draft review requires exact evidence linkage"
            )
        permit_patch.update({
            "draftReviewEvidenceRef": terminal_review_ref,
            "draftReviewEvidenceHash": terminal_review_hash,
        })

    settlement_state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event=f"terminal_{permit_settlement}",
        now=now,
    )
    permit_write = {
        **pre_settlement_state_patch,
        **settlement_state_patch,
    }
    for document_ref, payload in deferred_sets:
        transaction.set(document_ref, payload)
    for document_ref in deferred_deletes:
        transaction.delete(document_ref)
    if capabilityless_projection:
        _enqueue_validated_orphaned_draft_permit_write(
            transaction,
            permit_ref,
            capabilityless_source_permit,
            pre_settlement_state_patch,
            settlement_state_patch,
        )
    else:
        transaction.update(permit_ref, settlement_state_patch)
    transaction.update(thread_ref, dict(thread_patch))
    if capabilityless_source_permit is not None:
        target_permit = _validate_permit({
            **capabilityless_source_permit,
            **permit_write,
        })
        target_thread = {**thread_data, **dict(thread_patch)}
        expected_review_hash = (
            _stable_evidence_hash(terminal_review_payload)
            if terminal_review_payload is not None
            else None
        )

        def capabilityless_readback_state():
            read_transaction = firestore_client.transaction()
            read_thread_snapshot = thread_ref.get(
                transaction=read_transaction
            )
            read_claim_snapshot = (
                read_thread_snapshot
                if same_claim_root
                else claim_ref.get(transaction=read_transaction)
            )
            read_permit_snapshot = permit_ref.get(
                transaction=read_transaction
            )
            read_review_snapshot = (
                terminal_review_ref.get(transaction=read_transaction)
                if terminal_review_ref is not None
                else None
            )
            read_pending_snapshot = (
                terminal_pending_ref.get(transaction=read_transaction)
                if terminal_pending_ref is not None
                else None
            )
            if (
                not read_thread_snapshot.exists
                or not read_claim_snapshot.exists
                or not read_permit_snapshot.exists
            ):
                raise GraphSendPermitError(
                    "capability-less terminal settlement readback roots are missing"
                )
            read_thread = read_thread_snapshot.to_dict() or {}
            read_claim = read_claim_snapshot.to_dict() or {}
            _validate_terminal_claim_identity(
                read_claim,
                saga,
                issuer_owner,
                issuer_fence,
                now=datetime.now(timezone.utc),
                require_active_lease=True,
            )
            read_permit = _validate_permit(
                read_permit_snapshot.to_dict() or {}
            )
            target_claim_is_exact = (
                read_thread.get("terminalSagaClaim")
                == claim_data.get("terminalSagaClaim")
                if same_claim_root
                else read_claim == claim_data
            )
            target_review_is_exact = (
                terminal_review_ref is None
                or (
                    read_review_snapshot.exists
                    and _stable_evidence_hash(
                        read_review_snapshot.to_dict() or {}
                    )
                    == expected_review_hash
                )
            )
            source_review_is_exact = (
                terminal_review_ref is None
                or not read_review_snapshot.exists
            )
            target_pending_is_exact = (
                terminal_pending_ref is None
                or (
                    read_pending_snapshot.exists
                    and terminal_pending_target_data is not None
                    and (
                        read_pending_snapshot.to_dict()
                        == terminal_pending_target_data
                        if terminal_pending_source_exists
                        else _pending_settlement_commit_comparable(
                            read_pending_snapshot.to_dict() or {},
                            created_at_server_owned=True,
                        )
                        == _pending_settlement_commit_comparable(
                            terminal_pending_target_data,
                            created_at_server_owned=True,
                        )
                    )
                )
            )
            source_pending_is_exact = (
                terminal_pending_ref is None
                or (
                    read_pending_snapshot.exists
                    == terminal_pending_source_exists
                    and (
                        not terminal_pending_source_exists
                        or read_pending_snapshot.to_dict()
                        == terminal_pending_source_data
                    )
                )
            )
            if (
                _local_transition_comparable(read_permit)
                == _local_transition_comparable(target_permit)
                and _terminal_thread_commit_comparable(read_thread)
                == _terminal_thread_commit_comparable(target_thread)
                and target_claim_is_exact
                and target_review_is_exact
                and target_pending_is_exact
            ):
                return "target"
            if (
                read_permit == capabilityless_source_permit
                and read_thread == thread_data
                and read_claim == claim_data
                and source_review_is_exact
                and source_pending_is_exact
            ):
                return "source"
            raise GraphSendPermitError(
                "capability-less terminal settlement readback drifted"
            )

        def enqueue_capabilityless_retry():
            retry_transaction = firestore_client.transaction()
            retry_thread_snapshot = thread_ref.get(
                transaction=retry_transaction
            )
            retry_claim_snapshot = (
                retry_thread_snapshot
                if same_claim_root
                else claim_ref.get(transaction=retry_transaction)
            )
            retry_permit_snapshot = permit_ref.get(
                transaction=retry_transaction
            )
            retry_review_snapshot = (
                terminal_review_ref.get(transaction=retry_transaction)
                if terminal_review_ref is not None
                else None
            )
            retry_pending_snapshot = (
                terminal_pending_ref.get(transaction=retry_transaction)
                if terminal_pending_ref is not None
                else None
            )
            retry_review_source_is_exact = (
                terminal_review_ref is None
                or not retry_review_snapshot.exists
            )
            retry_pending_source_is_exact = (
                terminal_pending_ref is None
                or (
                    retry_pending_snapshot.exists
                    == terminal_pending_source_exists
                    and (
                        not terminal_pending_source_exists
                        or retry_pending_snapshot.to_dict()
                        == terminal_pending_source_data
                    )
                )
            )
            if (
                not retry_thread_snapshot.exists
                or not retry_claim_snapshot.exists
                or not retry_permit_snapshot.exists
                or retry_thread_snapshot.to_dict() != thread_data
                or retry_claim_snapshot.to_dict() != claim_data
                or retry_permit_snapshot.to_dict()
                != capabilityless_source_permit
                or not retry_review_source_is_exact
                or not retry_pending_source_is_exact
            ):
                raise GraphSendPermitError(
                    "capability-less terminal retry source drifted"
                )
            _validate_terminal_claim_identity(
                retry_claim_snapshot.to_dict() or {},
                saga,
                issuer_owner,
                issuer_fence,
                now=datetime.now(timezone.utc),
                require_active_lease=True,
            )
            if terminal_pending_ref is not None and (
                not terminal_pending_source_exists
            ):
                retry_transaction.set(
                    terminal_pending_ref,
                    terminal_pending_target_data,
                )
            if terminal_review_ref is not None:
                retry_transaction.set(
                    terminal_review_ref,
                    terminal_review_payload,
                )
            if capabilityless_projection:
                _enqueue_validated_orphaned_draft_permit_write(
                    retry_transaction,
                    permit_ref,
                    capabilityless_source_permit,
                    pre_settlement_state_patch,
                    settlement_state_patch,
                )
            else:
                retry_transaction.update(
                    permit_ref,
                    settlement_state_patch,
                )
            retry_transaction.update(thread_ref, dict(thread_patch))
            return retry_transaction

        _commit_exact_orphaned_draft_settlement(
            transaction,
            readback_state=capabilityless_readback_state,
            enqueue_retry=enqueue_capabilityless_retry,
            operation="capability-less terminal pre-send settlement",
        )
    else:
        transaction.commit()
    return True


def assert_terminal_reply_permit_settled(
    transaction,
    thread_ref,
    *,
    thread_data: Optional[Dict[str, Any]] = None,
) -> None:
    """Reject settlement/cleanup while any active Graph permit is unresolved."""
    if thread_data is None:
        thread_snapshot = thread_ref.get(transaction=transaction)
        if not thread_snapshot.exists:
            raise GraphSendPermitBlocked("terminal cleanup thread root is missing")
        thread_data = thread_snapshot.to_dict() or {}
    _permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
    if permit and permit.get("status") not in GRAPH_SEND_RESOLVED_STATUSES:
        raise GraphSendPermitBlocked(
            "terminal cleanup blocked by unresolved Graph send permit"
        )
    attempt = (thread_data or {}).get("terminalReplyAttempt")
    if not isinstance(attempt, dict):
        return
    permit_id = attempt.get("graphSendPermitId")
    permit_hash = attempt.get("graphSendPermitHash")
    if bool(permit_id) != bool(permit_hash):
        raise GraphSendPermitBlocked(
            "terminal reply attempt has partial Graph permit evidence"
        )
    if permit_id and (
        permit is None
        or permit.get("permitId") != permit_id
        or permit.get("immutableHash") != permit_hash
        or permit.get("status") not in GRAPH_SEND_RESOLVED_STATUSES
    ):
        raise GraphSendPermitBlocked(
            "terminal reply attempt Graph permit is not issuer-settled"
        )


def read_active_terminal_reply_permit(
    firestore_client,
    thread_ref,
    attempt: Dict[str, Any],
    saga: Dict[str, Any],
) -> Dict[str, Any]:
    """Read one retained terminal permit bound to the exact current saga."""
    transaction = firestore_client.transaction()
    thread_snapshot = thread_ref.get(transaction=transaction)
    if not thread_snapshot.exists:
        raise GraphSendPermitBlocked("terminal reply permit thread root is missing")
    thread_data = thread_snapshot.to_dict() or {}
    _permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
    if not isinstance(saga, dict):
        raise GraphSendPermitBlocked(
            "terminal reply permit requires the current immutable saga"
        )
    body_hash = _body_hash(_canonical_terminal_response_body(saga))
    recipient = str(saga.get("replyRecipient") or "").strip().lower()
    expected_envelope = {
        "sagaKey": saga.get("sagaKey"),
        "sagaImmutableHash": saga.get("immutableHash"),
        "sourceGraphMessageId": saga.get("sourceGraphMessageId"),
        "conversationId": saga.get("sourceConversationId"),
        "recipient": recipient,
        "bodyHash": body_hash,
    }
    thread_id = str(getattr(thread_ref, "id", None) or "").strip()
    claim_thread_id = str(
        (saga.get("finalizationPlan") or {}).get("claimThreadId") or ""
    ).strip()
    user_path = _thread_user_root_path(thread_ref)
    expected_issuer_path = (
        f"{user_path}/threads/{claim_thread_id}"
        if user_path is not None
        else f"__in_memory__/threads/{claim_thread_id}"
    )
    if (
        permit is None
        or not isinstance(attempt, dict)
        or attempt.get("graphSendPermitId") != permit.get("permitId")
        or attempt.get("graphSendPermitHash") != permit.get("immutableHash")
        or attempt.get("sagaKey") != saga.get("sagaKey")
        or attempt.get("sourceGraphMessageId")
        != saga.get("sourceGraphMessageId")
        or attempt.get("conversationId") != saga.get("sourceConversationId")
        or str(attempt.get("recipient") or "").strip().lower() != recipient
        or attempt.get("responseBodyHash") != body_hash
        or permit.get("issuerKind") != "terminal_saga"
        or permit.get("envelopeHash") != _hash(expected_envelope)
        or permit.get("sourceGraphMessageId")
        != saga.get("sourceGraphMessageId")
        or permit.get("conversationId") != saga.get("sourceConversationId")
        or str(permit.get("recipient") or "").strip().lower() != recipient
        or permit.get("bodyHash") != body_hash
        or permit.get("clientId") != saga.get("clientId")
        or permit.get("threadId") != thread_id
        or not thread_id
        or permit.get("issuerDocumentId") != claim_thread_id
        or not claim_thread_id
        or permit.get("issuerDocumentPath") != expected_issuer_path
    ):
        raise GraphSendPermitBlocked(
            "terminal reply attempt or retained permit does not match the current saga"
        )
    return permit


def reconcile_pending_graph_send_permit(
    firestore_client,
    thread_ref,
    pending_ref,
    loaded_data: Dict[str, Any],
    *,
    outcome: str,
    sent_evidence: Optional[Dict[str, Any]] = None,
    evidence_document=None,
    operator_audit_document=None,
    completion_document=None,
) -> bool:
    """Reconcile an expired pending issuer without recovering its secret.

    This path can settle exact Sent evidence or retire ambiguous *draft-only*
    work to manual review.  It never grants a provider-send capability.
    """
    if outcome not in {
        "sent",
        "draft_needs_review",
        "send_needs_review",
        "definitely_not_sent",
        "definitely_not_started",
    }:
        raise ValueError(f"unsupported pending takeover outcome: {outcome}")
    if evidence_document is None:
        raise ValueError("pending takeover requires deterministic evidence")
    if operator_audit_document is not None and outcome != "sent":
        raise ValueError(
            "operator provenance is only supported for exact-Sent settlement"
        )
    if outcome == "sent" and completion_document is None:
        raise GraphSendPermitBlocked(
            "pending exact-Sent settlement requires a completion obligation"
        )
    if outcome != "sent" and completion_document is not None:
        raise GraphSendPermitBlocked(
            "pending completion obligation is only valid for exact-Sent "
            "settlement"
        )
    evidence_ref, raw_evidence_payload = evidence_document
    completion_ref = None
    raw_completion_payload: Dict[str, Any] = {}
    if completion_document is not None:
        completion_ref, raw_completion_payload = completion_document
        if completion_ref is None:
            raise GraphSendPermitBlocked(
                "pending exact-Sent completion reference is missing"
            )
    audit_ref = None
    raw_audit_payload: Dict[str, Any] = {}
    if operator_audit_document is not None:
        audit_ref, raw_audit_payload = operator_audit_document
        if audit_ref is None:
            raise GraphSendPermitBlocked(
                "operator exact-Sent settlement requires an audit reference"
            )
    transaction = firestore_client.transaction()
    now = datetime.now(timezone.utc)
    thread_snapshot = thread_ref.get(transaction=transaction)
    pending_snapshot = pending_ref.get(transaction=transaction)
    audit_snapshot = (
        audit_ref.get(transaction=transaction)
        if audit_ref is not None
        else None
    )
    completion_snapshot = (
        completion_ref.get(transaction=transaction)
        if completion_ref is not None
        else None
    )
    prior_review_snapshot = None
    if not thread_snapshot.exists:
        raise GraphSendPermitBlocked("pending takeover thread root is missing")
    thread_data = thread_snapshot.to_dict() or {}
    permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
    if permit is None or permit_ref is None:
        raise GraphSendPermitBlocked(
            "pending takeover retained permit is missing"
        )
    pending_document_id = str(getattr(pending_ref, "id", None) or "")
    _require_canonical_ref_path(
        pending_ref,
        _pending_response_path(thread_ref, pending_document_id),
        label="pending takeover issuer reference",
    )
    current_pending = (
        pending_snapshot.to_dict() if pending_snapshot.exists else {}
    ) or {}
    retained_review_ref = current_pending.get("graphSendReviewEvidenceRef")
    retained_review_hash = str(
        current_pending.get("graphSendReviewEvidenceHash") or ""
    ).strip()
    expected_review_path = _pending_review_path(
        thread_ref,
        str(permit.get("permitId") or ""),
    )
    expected_draft_review_path = _pending_draft_review_path(
        thread_ref,
        str(permit.get("permitId") or ""),
    )
    expected_dead_letter_path = _pending_dead_letter_path(
        thread_ref,
        pending_ref,
        permit,
    )
    if outcome == "send_needs_review":
        _require_canonical_ref_path(
            evidence_ref,
            expected_review_path,
            label="pending Graph review reference",
        )
    elif outcome == "draft_needs_review":
        _require_canonical_ref_path(
            evidence_ref,
            expected_draft_review_path,
            label="pending retained draft review reference",
        )
    elif outcome == "sent" and retained_review_ref is not None:
        _require_canonical_ref_path(
            evidence_ref,
            expected_review_path,
            label="pending retained review reference",
        )
        if not _same_document_ref(retained_review_ref, evidence_ref):
            raise GraphSendPermitBlocked(
                "pending exact-Sent settlement must resolve the retained review document"
            )
        prior_review_snapshot = evidence_ref.get(transaction=transaction)
        prior_review_data = (
            prior_review_snapshot.to_dict()
            if prior_review_snapshot.exists
            else {}
        )
        if (
            not retained_review_hash
            or not prior_review_snapshot.exists
            or _stable_evidence_hash(prior_review_data)
            != retained_review_hash
            or permit.get("pendingSendReviewRequired") is not True
            or permit.get("pendingReconciliationEvidenceHash")
            != retained_review_hash
        ):
            raise GraphSendPermitBlocked(
                "pending retained review linkage is missing or drifted"
            )
    elif audit_ref is not None:
        _require_canonical_ref_path(
            evidence_ref,
            expected_review_path,
            label="operator retained review reference",
        )
        prior_review_snapshot = evidence_ref.get(transaction=transaction)
    else:
        actual_evidence_path = _canonical_ref_path(evidence_ref)
        if expected_dead_letter_path is not None and (
            actual_evidence_path
            not in {expected_dead_letter_path, expected_review_path}
        ):
            raise GraphSendPermitBlocked(
                "pending reconciliation evidence does not use a canonical path"
            )
    evidence_payload = {
        **dict(raw_evidence_payload or {}),
        "graphSendPermitId": permit.get("permitId"),
        "graphSendPermitHash": permit.get("immutableHash"),
        "sourceGraphMessageId": permit.get("sourceGraphMessageId"),
        "preparedEnvelopeHash": (
            permit.get("sendPreparedEnvelopeHash")
            or (permit.get("preparedEnvelope") or {}).get(
                "preparedEnvelopeHash"
            )
        ),
    }
    if outcome == "sent" and retained_review_hash:
        evidence_payload.update({
            "originalReconciliationEvidenceHash": retained_review_hash,
            "sendOutcomeUnknown": False,
            "retryAllowed": False,
        })
    target_status = {
        "sent": "settled_sent",
        "draft_needs_review": "settled_draft_needs_review",
        "send_needs_review": "needs_reconciliation",
        "definitely_not_sent": "settled_definitely_not_sent",
        "definitely_not_started": "settled_definitely_not_sent",
    }[outcome]

    exact_sent_evidence = None
    completion_payload = None
    persisted_completion = None
    if outcome == "sent":
        exact_sent_evidence = _validate_exact_terminal_sent_evidence(
            permit,
            sent_evidence,
        )
        if evidence_payload.get("alreadySent") is not True:
            raise GraphSendPermitBlocked(
                "pending exact-Sent evidence must identify an accepted send"
            )
        completion_ref, completion_payload = (
            _validate_pending_completion_side_document(
                thread_ref,
                pending_ref,
                current_pending if pending_snapshot.exists else loaded_data,
                permit,
                exact_sent_evidence,
                ((completion_ref, raw_completion_payload),),
            )
        )
        if completion_snapshot is not None and completion_snapshot.exists:
            persisted_completion = (
                validate_pending_completion_obligation_payload(
                    completion_snapshot.to_dict() or {},
                    document_id=str(
                        getattr(completion_ref, "id", None) or ""
                    ),
                    expected_user_id=_thread_user_id(thread_ref),
                )
            )

    audit_payload = None
    audit_hash = None
    original_reconciliation_evidence_hash = None
    if audit_ref is not None:
        normalized_settlement_id = str(
            raw_audit_payload.get("settlementId") or ""
        ).strip()
        normalized_operator = str(
            raw_audit_payload.get("operatorId") or ""
        ).strip()
        normalized_reason = str(
            raw_audit_payload.get("operatorReason") or ""
        ).strip()
        normalized_lookup_completed_at = _utc(
            raw_audit_payload.get("freshSentLookupCompletedAt")
        )
        normalized_resolved_at = _utc(raw_audit_payload.get("resolvedAt"))
        _require_fresh_sent_lookup(
            normalized_lookup_completed_at,
            now=now,
            pending_data=current_pending,
        )
        exact_sent_message_id = str(
            (exact_sent_evidence or {}).get("sentMessageId")
            or (exact_sent_evidence or {}).get("id")
            or (exact_sent_evidence or {}).get("internetMessageId")
            or ""
        ).strip()
        original_reconciliation_evidence_hash = str(
            raw_audit_payload.get("reconciliationEvidenceHash") or ""
        ).strip()
        retained_original_hash = (
            permit.get("operatorOriginalReconciliationEvidenceHash")
            or permit.get("pendingReconciliationEvidenceHash")
        )
        if (
            raw_audit_payload.get("version") != 1
            or raw_audit_payload.get("action")
            != "acknowledge_ambiguous_no_retry"
            or raw_audit_payload.get("requestedAction")
            != "acknowledge_ambiguous_no_retry"
            or raw_audit_payload.get("resolution") != "exact_sent"
            or raw_audit_payload.get("alreadySent") is not True
            or raw_audit_payload.get("retryAllowed") is not False
            or not normalized_settlement_id
            or not normalized_operator
            or not normalized_reason
            or normalized_lookup_completed_at is None
            or normalized_resolved_at != normalized_lookup_completed_at
            or str(raw_audit_payload.get("sentMessageId") or "").strip()
            != exact_sent_message_id
            or original_reconciliation_evidence_hash
            != retained_original_hash
            or (
                getattr(audit_ref, "id", normalized_settlement_id)
                != normalized_settlement_id
            )
        ):
            raise GraphSendPermitBlocked(
                "operator exact-Sent audit provenance is malformed or stale"
            )
        _require_canonical_ref_path(
            audit_ref,
            _operator_audit_path(thread_ref, normalized_settlement_id),
            label="operator settlement audit reference",
        )
        audit_payload = {
            "version": 1,
            "settlementId": normalized_settlement_id,
            "action": "acknowledge_ambiguous_no_retry",
            "requestedAction": "acknowledge_ambiguous_no_retry",
            "operatorId": normalized_operator,
            "operatorReason": normalized_reason,
            "threadId": permit.get("threadId"),
            "pendingDocumentId": getattr(pending_ref, "id", None),
            "graphSendPermitId": permit.get("permitId"),
            "graphSendPermitHash": permit.get("immutableHash"),
            "reconciliationEvidenceHash": (
                original_reconciliation_evidence_hash
            ),
            "reconciliationEvidenceRef": evidence_ref,
            "resolution": "exact_sent",
            "alreadySent": True,
            "retryAllowed": False,
            "freshSentLookupCompletedAt": normalized_lookup_completed_at,
            "resolvedAt": normalized_resolved_at,
            "sentMessageId": exact_sent_message_id,
            "sentDateTime": (exact_sent_evidence or {}).get("sentDateTime"),
        }
        audit_hash = _stable_evidence_hash(audit_payload)
        evidence_payload.update({
            "originalReconciliationEvidenceHash": (
                original_reconciliation_evidence_hash
            ),
            "operatorSettlementAuditRef": audit_ref,
            "operatorSettlementId": normalized_settlement_id,
            "operatorRequestedAction": "acknowledge_ambiguous_no_retry",
            "resolution": "exact_sent",
            "retryAllowed": False,
        })

    evidence_hash = _stable_evidence_hash(evidence_payload)

    if (
        not pending_snapshot.exists
        and outcome == "draft_needs_review"
        and permit.get("status") == "settled_draft_needs_review"
    ):
        evidence_payload = _orphaned_draft_review_payload(
            permit,
            evidence_payload,
        )
        evidence_hash = _stable_evidence_hash(evidence_payload)

    if not pending_snapshot.exists:
        evidence_snapshot = (
            prior_review_snapshot
            if prior_review_snapshot is not None
            else evidence_ref.get(transaction=transaction)
        )
        if (
            permit.get("status") == target_status
            and permit.get("pendingReconciliationEvidenceHash") == evidence_hash
            and evidence_snapshot.exists
            and _stable_evidence_hash(evidence_snapshot.to_dict() or {})
            == evidence_hash
            and (
                outcome != "sent"
                or (
                    persisted_completion is not None
                    and persisted_completion.get("immutable")
                    == completion_payload.get("immutable")
                    and persisted_completion.get("immutableHash")
                    == completion_payload.get("immutableHash")
                    and _stable_evidence_hash(
                        permit.get("terminalSentEvidence") or {}
                    )
                    == completion_payload["immutable"]["sentEvidenceHash"]
                )
            )
        ):
            if audit_ref is not None:
                existing_audit = (
                    audit_snapshot.to_dict() if audit_snapshot.exists else {}
                )
                existing_evidence = evidence_snapshot.to_dict() or {}
                if (
                    not audit_snapshot.exists
                    or _stable_evidence_hash(existing_audit) != audit_hash
                    or permit.get("operatorSettlementAuditHash") != audit_hash
                    or not _same_document_ref(
                        permit.get("operatorSettlementAuditRef"),
                        audit_ref,
                    )
                    or permit.get(
                        "operatorOriginalReconciliationEvidenceHash"
                    ) != original_reconciliation_evidence_hash
                    or permit.get("operatorResolvedReviewEvidenceHash")
                    != evidence_hash
                    or not _same_document_ref(
                        existing_evidence.get("operatorSettlementAuditRef"),
                        audit_ref,
                    )
                ):
                    raise GraphSendPermitBlocked(
                        "operator exact-Sent settlement audit is missing or drifted"
                    )
            return True
        raise GraphSendPermitBlocked(
            "pending takeover work disappeared before exact reconciliation"
        )
    current = current_pending
    exact_pending_document_id, exact_pending_document_path = (
        _issuer_document_identity(
            pending_ref,
            issuer_kind="pending_response",
        )
    )
    if (
        permit.get("issuerKind") != "pending_response"
        or permit.get("threadId")
        != str(getattr(thread_ref, "id", None) or "")
        or permit.get("issuerDocumentId")
        != exact_pending_document_id
        or permit.get("issuerDocumentPath")
        != exact_pending_document_path
        or pending_envelope_hash(current) != pending_envelope_hash(loaded_data)
        or pending_envelope_hash(current) != permit.get("envelopeHash")
        or current.get("processingBy") != permit.get("issuerOwner")
        or current.get("graphSendPermitId") != permit.get("permitId")
        or current.get("graphSendPermitHash") != permit.get("immutableHash")
    ):
        raise GraphSendPermitBlocked(
            "pending takeover document drifted from retained permit"
        )
    lease_until = _utc(current.get("processingLeaseUntil"))
    if lease_until is None or lease_until > now:
        raise GraphSendPermitBlocked(
            "pending takeover cannot replace an active or malformed issuer lease"
        )

    pre_settlement_state_patch: Dict[str, Any] = {}
    capabilityless_source_permit: Optional[Dict[str, Any]] = None
    capabilityless_projection = False
    recovery_kind = expired_graph_send_pre_send_recovery_kind(
        permit,
        now=now,
    )
    if (
        outcome == "draft_needs_review"
        and permit.get("status") == "issued"
        and recovery_kind == "draft_needs_review"
    ):
        capabilityless_source_permit = permit
        permit, pre_settlement_state_patch, _projection_event = (
            _lost_capability_pre_send_projection_patch(
                permit,
                now=now,
                outcome="draft_needs_review",
            )
        )
        capabilityless_projection = True
        evidence_payload = _orphaned_draft_review_payload(
            permit,
            evidence_payload,
        )
        evidence_hash = _stable_evidence_hash(evidence_payload)
    elif (
        outcome == "definitely_not_started"
        and recovery_kind == "definitely_not_started"
    ) or (
        outcome == "definitely_not_sent"
        and recovery_kind == "definitely_not_sent"
        and (
            _exact_definitely_not_created_resolution(permit)
            or _exact_preflight_definitely_not_sent_resolution(permit)
        )
    ):
        capabilityless_source_permit = permit

    capabilityless_evidence_snapshot = None
    if capabilityless_source_permit is not None:
        capabilityless_evidence_snapshot = evidence_ref.get(
            transaction=transaction
        )
        if capabilityless_evidence_snapshot.exists:
            raise GraphSendPermitBlocked(
                "capability-less pending settlement evidence id already exists"
            )

    preparation = dict(permit.get("draftPreparation") or {})
    delete_pending = True
    pending_patch = None
    if outcome == "sent":
        if completion_snapshot is not None and completion_snapshot.exists:
            raise GraphSendPermitBlocked(
                "pending completion obligation already exists before exact-Sent "
                "reconciliation"
            )
        if permit.get("status") not in {
            "request_started",
            "needs_reconciliation",
            "accepted",
            "reconciled_sent",
        }:
            raise GraphSendPermitBlocked(
                "pending takeover has no send-boundary provider state"
            )
        if retained_review_ref is not None:
            existing_review = (
                prior_review_snapshot.to_dict()
                if prior_review_snapshot is not None
                and prior_review_snapshot.exists
                else {}
            )
            if (
                permit.get("status") != "needs_reconciliation"
                or permit.get("pendingSendReviewRequired") is not True
                or not prior_review_snapshot.exists
                or _stable_evidence_hash(existing_review)
                != retained_review_hash
                or current.get("status") != "needs_reconciliation"
                or not _same_document_ref(
                    current.get("graphSendReviewEvidenceRef"),
                    evidence_ref,
                )
                or current.get("graphSendReviewEvidenceHash")
                != retained_review_hash
            ):
                raise GraphSendPermitBlocked(
                    "pending exact-Sent review evidence is not the retained ambiguity"
                )
        if audit_ref is not None:
            if audit_snapshot.exists:
                raise GraphSendPermitBlocked(
                    "operator exact-Sent audit id already exists with unresolved work"
                )
        permit_patch = {
            "status": "settled_sent",
            "issuerSettledAt": now,
            "terminalSentEvidence": exact_sent_evidence,
        }
        if retained_review_ref is not None:
            permit_patch["pendingSendReviewRequired"] = False
        if audit_ref is not None:
            permit_patch.update({
                "pendingSendReviewRequired": False,
                "operatorSettlementAuditRef": audit_ref,
                "operatorSettlementAuditHash": audit_hash,
                "operatorOriginalReconciliationEvidenceHash": (
                    original_reconciliation_evidence_hash
                ),
                "operatorResolvedReviewEvidenceHash": evidence_hash,
                "operatorResolution": "exact_sent",
            })
    elif outcome == "draft_needs_review":
        if (
            permit.get("requestStartedAt") is not None
            or permit.get("status") != "needs_reconciliation"
            or not _exact_pre_send_draft_resolution(permit)
            or evidence_payload.get("alreadySent") is not False
            or evidence_payload.get("providerSendStarted") is not False
        ):
            raise GraphSendPermitBlocked(
                "draft-only reconciliation cannot claim or imply a provider send"
            )
        evidence_hash = _validate_draft_review_evidence(
            permit,
            evidence_payload,
            expected_source="pendingGraphSendProtocol",
        )
        permit_patch = {
            "status": "settled_draft_needs_review",
            "issuerSettledAt": now,
            "draftReviewRequired": True,
            "draftReviewEvidenceRef": evidence_ref,
            "draftReviewEvidenceHash": evidence_hash,
        }
    elif outcome == "send_needs_review":
        if (
            permit.get("status")
            not in {"request_started", "needs_reconciliation", "accepted"}
            or permit.get("requestStartedAt") is None
            or "alreadySent" not in evidence_payload
            or evidence_payload.get("alreadySent") is not None
            or evidence_payload.get("providerSendStarted") is not True
            or evidence_payload.get("sendOutcomeUnknown") is not True
        ):
            raise GraphSendPermitBlocked(
                "ambiguous send review evidence does not match request_started"
            )
        permit_patch = {
            "status": target_status,
            "pendingSendReviewRequired": True,
        }
        prior_rechecks = current.get("graphSendSentRecheckCount", 0)
        if type(prior_rechecks) is not int or prior_rechecks < 0:
            raise GraphSendPermitBlocked(
                "pending send reconciliation recheck count is malformed"
            )
        if prior_rechecks >= PENDING_GRAPH_SENT_RECHECK_LIMIT:
            raise GraphSendPermitBlocked(
                "pending send reconciliation reached its read-only recheck cap"
            )
        delete_pending = False
        pending_patch = {
            "status": "needs_reconciliation",
            # Keep the exact expired issuer/permit links.  They deliberately
            # block a replacement send while later runs perform bounded,
            # read-only Sent Items checks or an authenticated operator settles
            # the ambiguity.
            "graphSendSentRecheckCount": prior_rechecks + 1,
            "graphSendSentLastCheckedAt": now,
            "graphSendReviewEvidenceRef": evidence_ref,
            "graphSendReviewEvidenceHash": evidence_hash,
            "updatedAt": SERVER_TIMESTAMP,
        }
    else:
        definitely_not_started = bool(
            outcome == "definitely_not_started"
            and recovery_kind == "definitely_not_started"
            and permit.get("status") == "issued"
            and not preparation
        )
        definitely_not_created = bool(
            outcome == "definitely_not_sent"
            and recovery_kind == "definitely_not_sent"
            and (
                _exact_definitely_not_created_resolution(permit)
                or _exact_preflight_definitely_not_sent_resolution(permit)
            )
        )
        if (
            not (definitely_not_started or definitely_not_created)
            or evidence_payload.get("alreadySent") is not False
            or evidence_payload.get("providerSendStarted") is not False
        ):
            raise GraphSendPermitBlocked(
                "pending permit lacks exact expired pre-send definite-unsent evidence"
            )
        permit_patch = {
            "status": "settled_definitely_not_sent",
            "issuerSettledAt": now,
        }
        delete_pending = False

    if audit_ref is not None:
        transaction.set(audit_ref, audit_payload)
    if completion_ref is not None:
        transaction.set(completion_ref, completion_payload)
    transaction.set(evidence_ref, evidence_payload)
    pending_exit_patch = pending_patch
    if delete_pending:
        transaction.delete(pending_ref)
    elif pending_exit_patch is not None:
        transaction.update(pending_ref, pending_exit_patch)
    else:
        pending_exit_patch = {
            "status": "queued",
            "processingBy": None,
            "processingAt": None,
            "processingLeaseUntil": None,
            "graphSendPermitId": None,
            "graphSendPermitHash": None,
            "updatedAt": SERVER_TIMESTAMP,
        }
        transaction.update(pending_ref, pending_exit_patch)
    finalized_permit_patch = {
        **permit_patch,
        "pendingReconciliationEvidenceHash": evidence_hash,
        "pendingReconciliationRecordedAt": now,
        "updatedAt": SERVER_TIMESTAMP,
    }
    settlement_state_patch = _stateful_permit_patch(
        permit,
        finalized_permit_patch,
        event=f"pending_reconcile_{outcome}",
        now=now,
    )
    permit_write = {
        **pre_settlement_state_patch,
        **settlement_state_patch,
    }
    if capabilityless_projection:
        _enqueue_validated_orphaned_draft_permit_write(
            transaction,
            permit_ref,
            capabilityless_source_permit,
            pre_settlement_state_patch,
            settlement_state_patch,
        )
    else:
        transaction.update(permit_ref, settlement_state_patch)
    if capabilityless_source_permit is not None:
        target_permit = _validate_permit({
            **capabilityless_source_permit,
            **permit_write,
        })
        expected_evidence_hash = _stable_evidence_hash(evidence_payload)
        target_pending = (
            None
            if delete_pending
            else {**current, **dict(pending_exit_patch or {})}
        )

        def capabilityless_readback_state():
            read_transaction = firestore_client.transaction()
            read_thread_snapshot = thread_ref.get(
                transaction=read_transaction
            )
            read_pending_snapshot = pending_ref.get(
                transaction=read_transaction
            )
            read_permit_snapshot = permit_ref.get(
                transaction=read_transaction
            )
            read_evidence_snapshot = evidence_ref.get(
                transaction=read_transaction
            )
            if (
                not read_thread_snapshot.exists
                or not read_permit_snapshot.exists
            ):
                raise GraphSendPermitError(
                    "capability-less pending settlement readback roots are missing"
                )
            read_thread = read_thread_snapshot.to_dict() or {}
            read_permit = _validate_permit(
                read_permit_snapshot.to_dict() or {}
            )
            target_pending_is_exact = (
                not read_pending_snapshot.exists
                if target_pending is None
                else (
                    read_pending_snapshot.exists
                    and _pending_settlement_commit_comparable(
                        read_pending_snapshot.to_dict() or {}
                    )
                    == _pending_settlement_commit_comparable(
                        target_pending
                    )
                )
            )
            if (
                read_thread == thread_data
                and target_pending_is_exact
                and _local_transition_comparable(read_permit)
                == _local_transition_comparable(target_permit)
                and read_evidence_snapshot.exists
                and _stable_evidence_hash(
                    read_evidence_snapshot.to_dict() or {}
                )
                == expected_evidence_hash
            ):
                return "target"
            if (
                read_thread == thread_data
                and read_pending_snapshot.exists
                and read_pending_snapshot.to_dict() == current
                and read_permit == capabilityless_source_permit
                and not read_evidence_snapshot.exists
            ):
                return "source"
            raise GraphSendPermitError(
                "capability-less pending settlement readback drifted"
            )

        def enqueue_capabilityless_retry():
            retry_transaction = firestore_client.transaction()
            retry_thread_snapshot = thread_ref.get(
                transaction=retry_transaction
            )
            retry_pending_snapshot = pending_ref.get(
                transaction=retry_transaction
            )
            retry_permit_snapshot = permit_ref.get(
                transaction=retry_transaction
            )
            retry_evidence_snapshot = evidence_ref.get(
                transaction=retry_transaction
            )
            if (
                not retry_thread_snapshot.exists
                or not retry_pending_snapshot.exists
                or not retry_permit_snapshot.exists
                or retry_thread_snapshot.to_dict() != thread_data
                or retry_pending_snapshot.to_dict() != current
                or retry_permit_snapshot.to_dict()
                != capabilityless_source_permit
                or retry_evidence_snapshot.exists
            ):
                raise GraphSendPermitError(
                    "capability-less pending retry source drifted"
                )
            retry_transaction.set(evidence_ref, evidence_payload)
            if delete_pending:
                retry_transaction.delete(pending_ref)
            else:
                retry_transaction.update(
                    pending_ref,
                    pending_exit_patch,
                )
            if capabilityless_projection:
                _enqueue_validated_orphaned_draft_permit_write(
                    retry_transaction,
                    permit_ref,
                    capabilityless_source_permit,
                    pre_settlement_state_patch,
                    settlement_state_patch,
                )
            else:
                retry_transaction.update(
                    permit_ref,
                    settlement_state_patch,
                )
            return retry_transaction

        _commit_exact_orphaned_draft_settlement(
            transaction,
            readback_state=capabilityless_readback_state,
            enqueue_retry=enqueue_capabilityless_retry,
            operation="capability-less pending pre-send settlement",
        )
    else:
        transaction.commit()
    return True


def operator_resolve_pending_graph_draft_review(
    firestore_client,
    thread_ref,
    *,
    expected_permit_id: str,
    expected_permit_hash: str,
    expected_review_evidence_hash: str,
    review_ref,
    action: str,
    operator_id: str,
    operator_reason: str,
    settlement_id: str,
    audit_ref,
) -> bool:
    """Resolve one retained draft only after an authenticated local attestation.

    The caller supplies its verified UID as ``operator_id``.  This exact CAS
    records that the already-retained provider draft is no longer actionable;
    it never calls Graph, deletes a draft, or itself issues a replacement send
    capability.  A later issuer may proceed only after this hashed transition
    is readable and valid.
    """
    if action != "confirm_retained_draft_not_actionable":
        raise ValueError(f"unsupported pending draft review action: {action}")
    normalized_operator = str(operator_id or "").strip()
    normalized_reason = str(operator_reason or "").strip()
    normalized_settlement_id = str(settlement_id or "").strip()
    expected_review_hash = str(
        expected_review_evidence_hash or ""
    ).strip().lower()
    if (
        not normalized_operator
        or not normalized_reason
        or not normalized_settlement_id
        or not expected_review_hash
    ):
        raise GraphSendPermitBlocked(
            "draft review resolution requires an authenticated actor, reason, "
            "settlement id, and exact review hash"
        )
    if review_ref is None or audit_ref is None:
        raise GraphSendPermitBlocked(
            "draft review resolution requires exact review and audit references"
        )
    _require_canonical_ref_path(
        review_ref,
        _pending_draft_review_path(thread_ref, expected_permit_id),
        label="operator pending draft review reference",
    )
    _require_canonical_ref_path(
        audit_ref,
        _draft_review_operator_audit_path(
            thread_ref,
            normalized_settlement_id,
        ),
        label="operator draft review settlement audit reference",
    )
    if (
        _canonical_ref_path(audit_ref) is not None
        and str(getattr(audit_ref, "id", None) or "")
        != normalized_settlement_id
    ):
        raise GraphSendPermitBlocked(
            "draft review settlement audit id is not exact"
        )

    transaction = firestore_client.transaction()
    now = datetime.now(timezone.utc)
    thread_snapshot = thread_ref.get(transaction=transaction)
    review_snapshot = review_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not thread_snapshot.exists or not review_snapshot.exists:
        raise GraphSendPermitBlocked(
            "draft review resolution exact thread or review is missing"
        )
    thread_data = thread_snapshot.to_dict() or {}
    permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
    if permit is None or permit_ref is None:
        raise GraphSendPermitBlocked(
            "draft review resolution retained permit is missing"
        )
    if (
        permit.get("permitId") != expected_permit_id
        or permit.get("immutableHash") != expected_permit_hash
        or permit.get("issuerKind") != "pending_response"
    ):
        raise GraphSendPermitBlocked(
            "draft review resolution retained permit identity is not exact"
        )

    if permit.get("status") == "settled_draft_review_resolved":
        audit = audit_snapshot.to_dict() if audit_snapshot.exists else {}
        resolved_review = review_snapshot.to_dict() or {}
        if (
            permit.get("operatorOriginalReconciliationEvidenceHash")
            == expected_review_hash
            and _same_document_ref(
                permit.get("operatorSettlementAuditRef"),
                audit_ref,
            )
            and permit.get("operatorSettlementAuditHash")
            == _stable_evidence_hash(audit or {})
            and permit.get("operatorResolvedReviewEvidenceHash")
            == _stable_evidence_hash(resolved_review)
            and audit.get("settlementId") == normalized_settlement_id
            and audit.get("operatorId") == normalized_operator
            and audit.get("operatorReason") == normalized_reason
            and resolved_review.get("originalReviewEvidenceHash")
            == expected_review_hash
        ):
            return True
        raise GraphSendPermitBlocked(
            "draft review resolution replay drifted from retained evidence"
        )

    current_review = review_snapshot.to_dict() or {}
    if (
        permit.get("status") != "settled_draft_needs_review"
        or permit.get("draftReviewRequired") is not True
        or not _same_document_ref(
            permit.get("draftReviewEvidenceRef"),
            review_ref,
        )
        or permit.get("draftReviewEvidenceHash") != expected_review_hash
        or permit.get("pendingReconciliationEvidenceHash")
        != expected_review_hash
        or _stable_evidence_hash(current_review) != expected_review_hash
        or audit_snapshot.exists
    ):
        raise GraphSendPermitBlocked(
            "draft review resolution permit, review, or audit id is not exact"
        )
    _validate_draft_review_evidence(
        permit,
        current_review,
        expected_source="pendingGraphSendProtocol",
    )

    audit_payload = {
        "version": 1,
        "settlementId": normalized_settlement_id,
        "action": action,
        "operatorId": normalized_operator,
        "operatorReason": normalized_reason,
        "threadId": permit.get("threadId"),
        "clientId": permit.get("clientId"),
        "pendingDocumentId": permit.get("issuerDocumentId"),
        "graphSendPermitId": permit.get("permitId"),
        "graphSendPermitHash": permit.get("immutableHash"),
        "reviewEvidenceHash": expected_review_hash,
        "reviewEvidenceRef": review_ref,
        "resolution": "retained_draft_not_actionable",
        "providerSendStarted": False,
        "automaticDeleteAttempted": False,
        "retryAllowed": True,
        "resolvedAt": now,
    }
    audit_hash = _stable_evidence_hash(audit_payload)
    resolved_review = {
        **current_review,
        "status": "resolved_not_actionable",
        "resolution": "retained_draft_not_actionable",
        "retryAllowed": True,
        "originalReviewEvidenceHash": expected_review_hash,
        "operatorSettlementAuditRef": audit_ref,
        "operatorSettlementId": normalized_settlement_id,
        "resolvedBy": normalized_operator,
        "operatorReason": normalized_reason,
        "resolvedAt": now,
        "updatedAt": now,
    }
    resolved_review_hash = _stable_evidence_hash(resolved_review)
    permit_patch = {
        "status": "settled_draft_review_resolved",
        "draftReviewRequired": False,
        "draftReviewEvidenceHash": resolved_review_hash,
        "operatorSettlementAuditRef": audit_ref,
        "operatorSettlementAuditHash": audit_hash,
        "operatorOriginalReconciliationEvidenceHash": expected_review_hash,
        "operatorResolvedReviewEvidenceHash": resolved_review_hash,
        "operatorResolution": "retained_draft_not_actionable",
        "updatedAt": SERVER_TIMESTAMP,
    }
    state_patch = _stateful_permit_patch(
        permit,
        permit_patch,
        event="operator_draft_review_resolved",
        now=now,
    )
    target_permit = {**permit, **state_patch}

    def exact_resolution_state(read_transaction) -> str:
        try:
            read_thread_snapshot = thread_ref.get(
                transaction=read_transaction
            )
            if read_thread_snapshot.exists is not True:
                raise GraphSendPermitError(
                    "operator draft review resolution thread disappeared"
                )
            read_thread = read_thread_snapshot.to_dict() or {}
            read_permit_ref, read_permit = _active_permit(
                read_transaction,
                thread_ref,
                read_thread,
            )
            read_review_snapshot = review_ref.get(
                transaction=read_transaction
            )
            read_audit_snapshot = audit_ref.get(
                transaction=read_transaction
            )
        except GraphSendPermitError as read_error:
            raise GraphSendPermitError(
                "operator draft review resolution readback is malformed"
            ) from read_error
        if (
            read_permit_ref is None
            or read_permit is None
            or not _same_document_ref(read_permit_ref, permit_ref)
            or read_thread != thread_data
        ):
            raise GraphSendPermitError(
                "operator draft review resolution readback drifted"
            )
        if (
            _local_transition_comparable(read_permit)
            == _local_transition_comparable(target_permit)
            and read_review_snapshot.exists is True
            and read_review_snapshot.to_dict() == resolved_review
            and read_audit_snapshot.exists is True
            and read_audit_snapshot.to_dict() == audit_payload
        ):
            return "target"
        if (
            read_permit == permit
            and read_review_snapshot.exists is True
            and read_review_snapshot.to_dict() == current_review
            and read_audit_snapshot.exists is False
        ):
            return "source"
        raise GraphSendPermitError(
            "operator draft review resolution readback drifted"
        )

    def readback_state() -> str:
        return exact_resolution_state(firestore_client.transaction())

    def enqueue_retry():
        retry_transaction = firestore_client.transaction()
        if exact_resolution_state(retry_transaction) != "source":
            raise GraphSendPermitError(
                "operator draft review resolution retry source drifted"
            )
        retry_transaction.set(audit_ref, audit_payload)
        retry_transaction.set(review_ref, resolved_review)
        retry_transaction.update(permit_ref, state_patch)
        return retry_transaction

    transaction.set(audit_ref, audit_payload)
    transaction.set(review_ref, resolved_review)
    transaction.update(permit_ref, state_patch)
    _commit_exact_orphaned_draft_settlement(
        transaction,
        readback_state=readback_state,
        enqueue_retry=enqueue_retry,
        operation="operator draft review resolution",
    )
    return True


def operator_settle_pending_graph_send_review(
    firestore_client,
    thread_ref,
    pending_ref,
    loaded_data: Dict[str, Any],
    *,
    expected_permit_id: str,
    expected_permit_hash: str,
    expected_reconciliation_evidence_hash: str,
    reconciliation_evidence_ref,
    action: str,
    operator_id: str,
    operator_reason: str,
    settlement_id: str,
    audit_ref,
    sent_lookup_completed_at: Any,
) -> bool:
    """Atomically retire one exact ambiguous send without claiming sent/unsent.

    Authentication is intentionally a caller responsibility; the production
    entry point is guarded by the revoked-token-checking Firebase decorator and
    passes only its verified UID as ``operator_id``.  This lower-level CAS then
    binds that actor to the retained permit, latest deterministic evidence, and
    immutable pending envelope.  It never authorizes a retry and never asserts
    whether the provider accepted the request.
    """
    if action != "acknowledge_ambiguous_no_retry":
        raise ValueError(f"unsupported pending send review action: {action}")
    normalized_operator = str(operator_id or "").strip()
    normalized_reason = str(operator_reason or "").strip()
    normalized_settlement_id = str(settlement_id or "").strip()
    normalized_lookup_completed_at = _utc(sent_lookup_completed_at)
    if not normalized_operator or not normalized_reason or not normalized_settlement_id:
        raise GraphSendPermitBlocked(
            "operator settlement requires an authenticated actor, reason, and id"
        )
    if normalized_lookup_completed_at is None:
        raise GraphSendPermitBlocked(
            "operator settlement requires a fresh readable Sent Items check"
        )
    if audit_ref is None or reconciliation_evidence_ref is None:
        raise GraphSendPermitBlocked(
            "operator settlement requires exact evidence and audit references"
        )
    _require_canonical_ref_path(
        pending_ref,
        _pending_response_path(
            thread_ref,
            str(getattr(pending_ref, "id", None) or ""),
        ),
        label="operator pending issuer reference",
    )
    _require_canonical_ref_path(
        reconciliation_evidence_ref,
        _pending_review_path(thread_ref, expected_permit_id),
        label="operator retained review reference",
    )
    _require_canonical_ref_path(
        audit_ref,
        _operator_audit_path(thread_ref, normalized_settlement_id),
        label="operator settlement audit reference",
    )
    if (
        _canonical_ref_path(audit_ref) is not None
        and str(getattr(audit_ref, "id", None) or "")
        != normalized_settlement_id
    ):
        raise GraphSendPermitBlocked(
            "operator settlement audit id is not the exact settlement id"
        )

    transaction = firestore_client.transaction()
    now = datetime.now(timezone.utc)
    thread_snapshot = thread_ref.get(transaction=transaction)
    pending_snapshot = pending_ref.get(transaction=transaction)
    audit_snapshot = audit_ref.get(transaction=transaction)
    evidence_snapshot = reconciliation_evidence_ref.get(transaction=transaction)
    if not thread_snapshot.exists or not evidence_snapshot.exists:
        raise GraphSendPermitBlocked(
            "operator settlement exact thread/evidence is missing"
        )
    thread_data = thread_snapshot.to_dict() or {}
    permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
    if permit is None or permit_ref is None:
        raise GraphSendPermitBlocked(
            "operator settlement retained permit is missing"
        )
    if (
        permit.get("permitId") != expected_permit_id
        or permit.get("immutableHash") != expected_permit_hash
        or permit.get("issuerKind") != "pending_response"
    ):
        raise GraphSendPermitBlocked(
            "operator settlement retained permit identity is not exact"
        )

    existing_evidence = evidence_snapshot.to_dict() or {}
    if not pending_snapshot.exists:
        existing_audit = audit_snapshot.to_dict() if audit_snapshot.exists else {}
        if (
            permit.get("status") == "settled_ambiguous_no_retry"
            and permit.get("operatorSettlementAuditHash")
            == _stable_evidence_hash(existing_audit or {})
            and permit.get("operatorResolvedReviewEvidenceHash")
            == _stable_evidence_hash(existing_evidence)
            and existing_audit.get("settlementId") == normalized_settlement_id
            and existing_audit.get("action") == action
            and existing_audit.get("operatorId") == normalized_operator
            and existing_audit.get("operatorReason") == normalized_reason
            and existing_audit.get("graphSendPermitId") == expected_permit_id
            and existing_audit.get("graphSendPermitHash") == expected_permit_hash
            and existing_audit.get("reconciliationEvidenceHash")
            == expected_reconciliation_evidence_hash
            and _utc(existing_audit.get("freshSentLookupCompletedAt"))
            == normalized_lookup_completed_at
            and existing_evidence.get("status")
            == "settled_ambiguous_no_retry"
            and existing_evidence.get("originalReconciliationEvidenceHash")
            == expected_reconciliation_evidence_hash
            and _same_document_ref(
                existing_evidence.get("operatorSettlementAuditRef"),
                audit_ref,
            )
        ):
            return True
        raise GraphSendPermitBlocked(
            "operator settlement pending work disappeared before exact CAS"
        )

    current = pending_snapshot.to_dict() or {}
    _require_fresh_sent_lookup(
        normalized_lookup_completed_at,
        now=now,
        pending_data=current,
    )

    if (
        permit.get("status") != "needs_reconciliation"
        or permit.get("pendingSendReviewRequired") is not True
        or permit.get("pendingReconciliationEvidenceHash")
        != expected_reconciliation_evidence_hash
        or _stable_evidence_hash(existing_evidence)
        != expected_reconciliation_evidence_hash
    ):
        raise GraphSendPermitBlocked(
            "operator settlement permit or reconciliation evidence is not exact"
        )

    audit_payload = {
        "version": 1,
        "settlementId": normalized_settlement_id,
        "action": action,
        "operatorId": normalized_operator,
        "operatorReason": normalized_reason,
        "threadId": permit.get("threadId"),
        "pendingDocumentId": getattr(pending_ref, "id", None),
        "graphSendPermitId": expected_permit_id,
        "graphSendPermitHash": expected_permit_hash,
        "reconciliationEvidenceHash": expected_reconciliation_evidence_hash,
        "reconciliationEvidenceRef": reconciliation_evidence_ref,
        "resolution": "unknown_no_retry",
        "alreadySent": None,
        "retryAllowed": False,
        "freshSentLookupCompletedAt": normalized_lookup_completed_at,
        "resolvedAt": now,
    }
    audit_hash = _stable_evidence_hash(audit_payload)

    if (
        pending_envelope_hash(current) != pending_envelope_hash(loaded_data)
        or pending_envelope_hash(current) != permit.get("envelopeHash")
        or current.get("processingBy") != permit.get("issuerOwner")
        or current.get("graphSendPermitId") != expected_permit_id
        or current.get("graphSendPermitHash") != expected_permit_hash
        or current.get("status") != "needs_reconciliation"
        or not _same_document_ref(
            current.get("graphSendReviewEvidenceRef"),
            reconciliation_evidence_ref,
        )
        or current.get("graphSendReviewEvidenceHash")
        != expected_reconciliation_evidence_hash
    ):
        raise GraphSendPermitBlocked(
            "operator settlement pending envelope, owner, or evidence is not exact"
        )
    lease_until = _utc(current.get("processingLeaseUntil"))
    if lease_until is None or lease_until > now:
        raise GraphSendPermitBlocked(
            "operator settlement cannot replace an active or malformed issuer lease"
        )
    if audit_snapshot.exists:
        raise GraphSendPermitBlocked(
            "operator settlement audit id already exists with unresolved work"
        )

    resolved_evidence = {
        **existing_evidence,
        "status": "settled_ambiguous_no_retry",
        "resolution": "unknown_no_retry",
        "alreadySent": None,
        "sendOutcomeUnknown": True,
        "retryAllowed": False,
        "originalReconciliationEvidenceHash": (
            expected_reconciliation_evidence_hash
        ),
        "operatorSettlementAuditRef": audit_ref,
        "operatorSettlementId": normalized_settlement_id,
        "resolvedBy": normalized_operator,
        "resolvedAt": now,
        "updatedAt": now,
    }
    resolved_evidence_hash = _stable_evidence_hash(resolved_evidence)
    transaction.set(audit_ref, audit_payload)
    transaction.set(reconciliation_evidence_ref, resolved_evidence)
    transaction.delete(pending_ref)
    permit_patch = {
        "status": "settled_ambiguous_no_retry",
        "issuerSettledAt": now,
        "pendingSendReviewRequired": False,
        "operatorSettlementAuditRef": audit_ref,
        "operatorSettlementAuditHash": audit_hash,
        "operatorOriginalReconciliationEvidenceHash": (
            expected_reconciliation_evidence_hash
        ),
        "operatorResolvedReviewEvidenceHash": resolved_evidence_hash,
        "operatorResolution": "unknown_no_retry",
        "updatedAt": SERVER_TIMESTAMP,
    }
    transaction.update(
        permit_ref,
        _stateful_permit_patch(
            permit,
            permit_patch,
            event="operator_ambiguous_no_retry",
            now=now,
        ),
    )
    transaction.commit()
    return True


def read_pending_graph_send_operator_settlement_replay(
    firestore_client,
    user_ref,
    audit_ref,
    *,
    pending_document_id: str,
    expected_permit_id: str,
    expected_permit_hash: str,
    expected_reconciliation_evidence_hash: str,
    operator_id: str,
    operator_reason: str,
    settlement_id: str,
) -> Optional[str]:
    """Return an exact prior operator result without mailbox or pending reads."""
    normalized_settlement_id = str(settlement_id or "").strip()
    user_path = _canonical_ref_path(user_ref)
    _require_canonical_ref_path(
        audit_ref,
        (
            f"{user_path}/graphSendOperatorSettlements/"
            f"{normalized_settlement_id}"
            if user_path is not None
            else None
        ),
        label="operator settlement replay audit reference",
    )
    transaction = firestore_client.transaction()
    audit_snapshot = audit_ref.get(transaction=transaction)
    if not audit_snapshot.exists:
        return None
    audit = audit_snapshot.to_dict() or {}
    resolution = audit.get("resolution")
    expected_status = {
        "exact_sent": "settled_sent",
        "unknown_no_retry": "settled_ambiguous_no_retry",
    }.get(resolution)
    thread_id = str(audit.get("threadId") or "").strip()
    if (
        expected_status is None
        or audit.get("version") != 1
        or audit.get("settlementId") != normalized_settlement_id
        or audit.get("action") != "acknowledge_ambiguous_no_retry"
        or audit.get("requestedAction", "acknowledge_ambiguous_no_retry")
        != "acknowledge_ambiguous_no_retry"
        or audit.get("operatorId") != str(operator_id or "").strip()
        or audit.get("operatorReason") != str(operator_reason or "").strip()
        or audit.get("pendingDocumentId") != pending_document_id
        or audit.get("graphSendPermitId") != expected_permit_id
        or audit.get("graphSendPermitHash") != expected_permit_hash
        or audit.get("reconciliationEvidenceHash")
        != expected_reconciliation_evidence_hash
        or audit.get("retryAllowed") is not False
        or not thread_id
        or (
            resolution == "exact_sent"
            and audit.get("alreadySent") is not True
        )
        or (
            resolution == "unknown_no_retry"
            and audit.get("alreadySent") is not None
        )
    ):
        raise GraphSendPermitBlocked(
            "operator settlement replay audit does not match the exact request"
        )
    thread_ref = user_ref.collection("threads").document(thread_id)
    evidence_ref = audit.get("reconciliationEvidenceRef")
    if evidence_ref is None:
        raise GraphSendPermitBlocked(
            "operator settlement replay audit has no resolved review reference"
        )
    _require_canonical_ref_path(
        evidence_ref,
        _pending_review_path(thread_ref, expected_permit_id),
        label="operator settlement replay review reference",
    )
    evidence_snapshot = evidence_ref.get(transaction=transaction)
    if not evidence_snapshot.exists:
        raise GraphSendPermitBlocked(
            "operator settlement replay resolved review is missing"
        )
    resolved_evidence = evidence_snapshot.to_dict() or {}
    permit_ref = thread_ref.collection("graphSendPermits").document(
        expected_permit_id
    )
    permit_snapshot = permit_ref.get(transaction=transaction)
    if not permit_snapshot.exists:
        raise GraphSendPermitBlocked(
            "operator settlement replay retained permit is missing"
        )
    permit = _validate_permit(permit_snapshot.to_dict() or {})
    retained_original_hash = (
        permit.get("operatorOriginalReconciliationEvidenceHash")
        or permit.get("pendingReconciliationEvidenceHash")
    )
    expected_evidence_status = {
        "exact_sent": "reconciled_sent",
        "unknown_no_retry": "settled_ambiguous_no_retry",
    }[resolution]
    if (
        permit.get("status") != expected_status
        or permit.get("permitId") != expected_permit_id
        or permit.get("immutableHash") != expected_permit_hash
        or retained_original_hash
        != expected_reconciliation_evidence_hash
        or not _same_document_ref(
            permit.get("operatorSettlementAuditRef"),
            audit_ref,
        )
        or permit.get("operatorSettlementAuditHash")
        != _stable_evidence_hash(audit)
        or not _same_document_ref(
            audit.get("reconciliationEvidenceRef"),
            evidence_ref,
        )
        or permit.get("operatorResolvedReviewEvidenceHash")
        != _stable_evidence_hash(resolved_evidence)
        or resolved_evidence.get("status") != expected_evidence_status
        or resolved_evidence.get("resolution") != resolution
        or resolved_evidence.get("originalReconciliationEvidenceHash")
        != expected_reconciliation_evidence_hash
        or resolved_evidence.get("retryAllowed") is not False
        or resolved_evidence.get("operatorSettlementId")
        != normalized_settlement_id
        or not _same_document_ref(
            resolved_evidence.get("operatorSettlementAuditRef"),
            audit_ref,
        )
        or (
            resolution == "exact_sent"
            and resolved_evidence.get("alreadySent") is not True
        )
        or (
            resolution == "unknown_no_retry"
            and resolved_evidence.get("alreadySent") is not None
        )
    ):
        raise GraphSendPermitBlocked(
            "operator settlement replay permit/audit/review linkage drifted"
        )
    return expected_status


def read_expired_pending_graph_send_permit(
    firestore_client,
    thread_ref,
    pending_ref,
    loaded_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Return exact retained permit only after its original queue lease expires."""
    _require_canonical_ref_path(
        pending_ref,
        _pending_response_path(
            thread_ref,
            str(getattr(pending_ref, "id", None) or ""),
        ),
        label="expired pending issuer reference",
    )
    transaction = firestore_client.transaction()
    now = datetime.now(timezone.utc)
    thread_snapshot = thread_ref.get(transaction=transaction)
    pending_snapshot = pending_ref.get(transaction=transaction)
    if not thread_snapshot.exists or not pending_snapshot.exists:
        return None
    thread_data = thread_snapshot.to_dict() or {}
    current = pending_snapshot.to_dict() or {}
    _permit_ref, permit = _active_permit(transaction, thread_ref, thread_data)
    if permit is None:
        return None
    exact_pending_document_id, exact_pending_document_path = (
        _issuer_document_identity(
            pending_ref,
            issuer_kind="pending_response",
        )
    )
    if (
        permit.get("issuerKind") != "pending_response"
        or permit.get("threadId")
        != str(getattr(thread_ref, "id", None) or "")
        or permit.get("issuerDocumentId")
        != exact_pending_document_id
        or permit.get("issuerDocumentPath")
        != exact_pending_document_path
        or pending_envelope_hash(current) != pending_envelope_hash(loaded_data)
        or pending_envelope_hash(current) != permit.get("envelopeHash")
        or current.get("processingBy") != permit.get("issuerOwner")
        or current.get("graphSendPermitId") != permit.get("permitId")
        or current.get("graphSendPermitHash") != permit.get("immutableHash")
    ):
        raise GraphSendPermitBlocked(
            "expired pending work drifted from its retained permit"
        )
    lease_until = _utc(current.get("processingLeaseUntil"))
    if lease_until is None or lease_until > now:
        raise GraphSendPermitBlocked(
            "pending retained permit still has an active or malformed issuer lease"
        )
    permit_lease_until = _utc(permit.get("leaseUntil"))
    preparation = dict(permit.get("draftPreparation") or {})
    preparation_state = preparation.get("state")
    capabilityless_pre_send_source = bool(
        permit.get("requestStartedAt") is None
        and (
            (
                permit.get("status") == "issued"
                and (
                    not preparation
                    or preparation_state
                    in (
                        _RETAINABLE_PRE_SEND_DRAFT_STATES
                        | set(_ORPHANED_DRAFT_REQUEST_EVENTS)
                    )
                )
            )
            or (
                permit.get("status") == "definitely_not_sent"
                and (
                    not preparation
                    or preparation_state
                    == "create_definitely_not_created"
                )
            )
        )
    )
    if (
        capabilityless_pre_send_source
        and (
            permit_lease_until is None
            or permit_lease_until > now
        )
    ):
        raise GraphSendPermitBlocked(
            "pending capability-less pre-send work still has an active permit lease"
        )
    return permit


def _validate_pending_completion_side_document(
    thread_ref,
    pending_ref,
    current: Dict[str, Any],
    permit: Dict[str, Any],
    exact_sent_evidence: Dict[str, Any],
    side_documents,
):
    """Require the sole settled-sent side write to be the exact tombstone."""
    if len(side_documents) != 1:
        raise GraphSendPermitBlocked(
            "pending settled_sent requires exactly one completion obligation"
        )
    completion_ref, raw_payload = side_documents[0]
    payload = validate_pending_completion_obligation_payload(
        dict(raw_payload or {}),
        document_id=str(getattr(completion_ref, "id", None) or ""),
        expected_user_id=_thread_user_id(thread_ref),
    )
    immutable = payload["immutable"]
    canonical_thread_id = str(getattr(thread_ref, "id", None) or "")
    pending_document_id = str(getattr(pending_ref, "id", None) or "")
    user_id = _thread_user_id(thread_ref) or immutable.get("userId")
    raw_client_id = current.get("clientId")
    canonical_client_id = (
        "" if raw_client_id is None else str(raw_client_id)
    )
    expected_id, expected_payload = pending_completion_obligation_payload(
        user_id=user_id,
        client_id=canonical_client_id,
        thread_id=canonical_thread_id,
        pending_document_id=pending_document_id,
        source_graph_message_id=str(current.get("msgId") or ""),
        pending_envelope_hash_value=pending_envelope_hash(current),
        permit_id=str(permit.get("permitId") or ""),
        permit_immutable_hash=str(permit.get("immutableHash") or ""),
        sent_evidence=exact_sent_evidence,
        complete_client_after_reply=bool(canonical_client_id),
    )
    canonical_current_strings = {
        field_name: str(current.get(field_name) or "")
        for field_name in ("threadId", "msgId")
    }
    expected_pending_path = _pending_response_path(
        thread_ref,
        pending_document_id,
    )
    if (
        any(
            not value
            or value != value.strip()
            for value in canonical_current_strings.values()
        )
        or canonical_client_id != canonical_client_id.strip()
        or str(current.get("threadId") or "") != canonical_thread_id
        or permit.get("issuerKind") != "pending_response"
        or permit.get("issuerDocumentId") != pending_document_id
        or (
            expected_pending_path is not None
            and permit.get("issuerDocumentPath") != expected_pending_path
        )
        or permit.get("threadId") != canonical_thread_id
        or str(permit.get("clientId") or "") != canonical_client_id
        or permit.get("sourceGraphMessageId") != current.get("msgId")
        or permit.get("envelopeHash") != pending_envelope_hash(current)
        or payload != expected_payload
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation drifted from exact sent issuer"
        )
    _require_canonical_ref_path(
        completion_ref,
        _pending_completion_path(thread_ref, expected_id),
        label="pending completion obligation reference",
    )
    return completion_ref, payload


def cas_pending_claim_transition(
    firestore_client,
    thread_ref,
    pending_ref,
    loaded_data: Dict[str, Any],
    claim_token: str,
    *,
    pending_patch: Optional[Dict[str, Any]] = None,
    delete_pending: bool = False,
    side_documents=(),
    capability: Optional[GraphSendCapability] = None,
    permit_settlement: Optional[str] = None,
    sent_evidence: Optional[Dict[str, Any]] = None,
) -> bool:
    """Apply one post-claim exit without ever touching replacement work."""
    if bool(pending_patch) == bool(delete_pending):
        raise ValueError("pending CAS requires exactly one update or delete action")
    side_documents = tuple(side_documents)
    transaction = firestore_client.transaction()
    now = datetime.now(timezone.utc)
    thread_snapshot = thread_ref.get(transaction=transaction)
    pending_snapshot = pending_ref.get(transaction=transaction)
    if not thread_snapshot.exists:
        return False
    if not pending_snapshot.exists:
        if permit_settlement == "settled_sent":
            if (
                capability is None
                or not delete_pending
                or pending_patch is not None
                or capability.issuer_kind != "pending_response"
                or capability.issuer_owner != claim_token
                or not _same_document_ref(capability.thread_ref, thread_ref)
                or not _same_document_ref(capability.issuer_ref, pending_ref)
                or capability.envelope_hash
                != pending_envelope_hash(loaded_data)
            ):
                raise GraphSendPermitBlocked(
                    "pending settled_sent replay lost its exact issuer binding"
                )
            permit = _read_exact_capability_permit(transaction, capability)
            exact_sent_evidence = _validate_exact_terminal_sent_evidence(
                permit,
                sent_evidence,
            )
            completion_ref, completion_payload = (
                _validate_pending_completion_side_document(
                    thread_ref,
                    pending_ref,
                    loaded_data,
                    permit,
                    exact_sent_evidence,
                    side_documents,
                )
            )
            completion_snapshot = completion_ref.get(transaction=transaction)
            persisted_completion = (
                completion_snapshot.to_dict()
                if completion_snapshot.exists
                else {}
            )
            validated_persisted = (
                validate_pending_completion_obligation_payload(
                    persisted_completion,
                    document_id=str(
                        getattr(completion_ref, "id", None) or ""
                    ),
                    expected_user_id=_thread_user_id(thread_ref),
                )
                if completion_snapshot.exists
                else None
            )
            if (
                permit.get("status") == "settled_sent"
                and _stable_evidence_hash(
                    permit.get("terminalSentEvidence") or {}
                )
                == completion_payload["immutable"]["sentEvidenceHash"]
                and validated_persisted is not None
                and validated_persisted.get("immutable")
                == completion_payload.get("immutable")
                and validated_persisted.get("immutableHash")
                == completion_payload.get("immutableHash")
            ):
                return True
            raise GraphSendPermitBlocked(
                "pending settled_sent replay completion tombstone is missing "
                "or drifted"
            )
        if (
            capability is None
            or permit_settlement != "settled_draft_needs_review"
            or len(side_documents) != 1
            or not delete_pending
            or pending_patch is not None
            or capability.issuer_kind != "pending_response"
            or capability.issuer_owner != claim_token
            or not _same_document_ref(capability.thread_ref, thread_ref)
            or not _same_document_ref(capability.issuer_ref, pending_ref)
            or capability.envelope_hash != pending_envelope_hash(loaded_data)
        ):
            return False
        permit = _read_exact_capability_permit(transaction, capability)
        review_ref, raw_review_payload = side_documents[0]
        review_payload = dict(raw_review_payload or {})
        _require_canonical_ref_path(
            review_ref,
            _pending_draft_review_path(
                thread_ref,
                str(permit.get("permitId") or ""),
            ),
            label="pending retained draft review reference",
        )
        review_hash = _validate_draft_review_evidence(
            permit,
            review_payload,
            expected_source="pendingGraphSendProtocol",
        )
        review_snapshot = review_ref.get(transaction=transaction)
        persisted_review = (
            review_snapshot.to_dict() if review_snapshot.exists else {}
        )
        if (
            permit.get("status") == "settled_draft_needs_review"
            and permit.get("draftReviewEvidenceHash") == review_hash
            and permit.get("pendingReconciliationEvidenceHash") == review_hash
            and _same_document_ref(
                permit.get("draftReviewEvidenceRef"),
                review_ref,
            )
            and review_snapshot.exists
            and _stable_evidence_hash(persisted_review) == review_hash
        ):
            return True
        raise GraphSendPermitBlocked(
            "pending retained draft review replay is missing or drifted"
        )
    current = pending_snapshot.to_dict() or {}
    _require_canonical_ref_path(
        pending_ref,
        _pending_response_path(
            thread_ref,
            str(getattr(pending_ref, "id", None) or ""),
        ),
        label="pending claim issuer reference",
    )
    _validate_pending_claim(
        current,
        loaded_data,
        claim_token,
        now=now,
        # Once this exact capability has crossed the provider boundary, its
        # original issuer must be able to persist the provider outcome even if
        # the queue lease expires while the HTTP response is in flight.  The
        # active unresolved permit blocks every replacement worker meanwhile.
        require_active_lease=capability is None,
    )

    if capability is None:
        linked_permit_id = current.get("graphSendPermitId")
        linked_permit_hash = current.get("graphSendPermitHash")
        if linked_permit_id is not None or linked_permit_hash is not None:
            raise GraphSendPermitBlocked(
                "capability-less pending exit cannot release a linked Graph "
                "send permit"
            )
        _active_ref, active_permit = _active_permit(
            transaction,
            thread_ref,
            thread_snapshot.to_dict() or {},
        )
        if graph_send_permit_blocks_new_send(active_permit):
            raise GraphSendPermitBlocked(
                "capability-less pending exit cannot release work while an "
                "active Graph send permit is unresolved"
            )

    if capability is not None:
        if (
            capability.issuer_kind != "pending_response"
            or capability.issuer_owner != claim_token
            or not _same_document_ref(capability.thread_ref, thread_ref)
            or not _same_document_ref(capability.issuer_ref, pending_ref)
            or current.get("graphSendPermitId") != capability.permit_id
            or current.get("graphSendPermitHash") != capability.immutable_hash
            or pending_envelope_hash(current) != capability.envelope_hash
        ):
            raise GraphSendPermitBlocked(
                "pending exit capability does not belong to the exact claim"
            )
        permit = _read_exact_capability_permit(transaction, capability)
        settlement_sources = {
            "settled_sent": {"accepted", "reconciled_sent"},
            "settled_definitely_not_sent": {"definitely_not_sent"},
        }
        if permit_settlement == "settled_draft_needs_review":
            if not delete_pending or pending_patch is not None or len(side_documents) != 1:
                raise GraphSendPermitBlocked(
                    "pending draft review must retire one exact issuer with one review"
                )
            review_ref, raw_review_payload = side_documents[0]
            review_payload = dict(raw_review_payload or {})
            _require_canonical_ref_path(
                review_ref,
                _pending_draft_review_path(
                    thread_ref,
                    str(permit.get("permitId") or ""),
                ),
                label="pending retained draft review reference",
            )
            review_hash = _validate_draft_review_evidence(
                permit,
                review_payload,
                expected_source="pendingGraphSendProtocol",
            )
            permit_patch = {
                "status": "settled_draft_needs_review",
                "issuerSettledAt": now,
                "draftReviewRequired": True,
                "draftReviewEvidenceRef": review_ref,
                "draftReviewEvidenceHash": review_hash,
                "pendingReconciliationEvidenceHash": review_hash,
                "pendingReconciliationRecordedAt": now,
                "updatedAt": SERVER_TIMESTAMP,
            }
            transaction.update(
                capability.permit_ref,
                _stateful_permit_patch(
                    permit,
                    permit_patch,
                    event="pending_reconcile_draft_needs_review",
                    now=now,
                ),
            )
        elif permit_settlement == "reconciliation_recorded":
            current_status = permit.get("status")
            if current_status in {
                "accepted",
                "request_started",
                "needs_reconciliation",
            }:
                if delete_pending or len(side_documents) != 1:
                    raise GraphSendPermitBlocked(
                        "pending ambiguous send must retain its exact issuer and one review"
                    )
                review_ref, review_payload = side_documents[0]
                review_payload = dict(review_payload or {})
                _require_canonical_ref_path(
                    review_ref,
                    _pending_review_path(
                        thread_ref,
                        str(permit.get("permitId") or ""),
                    ),
                    label="pending ambiguity review reference",
                )
                if (
                    review_payload.get("alreadySent") is not None
                    or review_payload.get("providerSendStarted") is not True
                    or review_payload.get("sendOutcomeUnknown") is not True
                    or review_payload.get("retryAllowed") is not False
                    or review_payload.get("graphSendPermitId")
                    != permit.get("permitId")
                    or review_payload.get("graphSendPermitHash")
                    != permit.get("immutableHash")
                ):
                    raise GraphSendPermitBlocked(
                        "pending ambiguous send review is not exact tri-state protocol work"
                    )
                review_hash = _stable_evidence_hash(review_payload)
                if (
                    not isinstance(pending_patch, dict)
                    or pending_patch.get("status") != "needs_reconciliation"
                ):
                    raise GraphSendPermitBlocked(
                        "pending ambiguous send must remain reconciliation-only"
                    )
                protected_pending_fields = {
                    "threadId",
                    "msgId",
                    "recipient",
                    "responseBody",
                    "clientId",
                    "conversationId",
                    "processingBy",
                    "processingAt",
                    "graphSendPermitId",
                    "graphSendPermitHash",
                }
                if any(
                    field_name in pending_patch
                    and pending_patch.get(field_name) != current.get(field_name)
                    for field_name in protected_pending_fields
                ):
                    raise GraphSendPermitBlocked(
                        "pending ambiguity patch cannot change claim or immutable envelope fields"
                    )
                pending_patch = {
                    **pending_patch,
                    "processingLeaseUntil": now,
                    "graphSendReviewEvidenceRef": review_ref,
                    "graphSendReviewEvidenceHash": review_hash,
                }
                permit_patch = {
                    "status": "needs_reconciliation",
                    "reconciliationRecordedAt": now,
                    "pendingSendReviewRequired": True,
                    "pendingReconciliationEvidenceHash": review_hash,
                    "pendingReconciliationRecordedAt": now,
                    "updatedAt": SERVER_TIMESTAMP,
                }
                transaction.update(
                    capability.permit_ref,
                    _stateful_permit_patch(
                        permit,
                        permit_patch,
                        event="pending_reconciliation_recorded",
                        now=now,
                    ),
                )
            else:
                raise GraphSendPermitBlocked(
                    "pending reconciliation exit has no ambiguous or sent permit"
                )
        else:
            if (
                permit_settlement not in settlement_sources
                or permit.get("status") not in settlement_sources[permit_settlement]
            ):
                raise GraphSendPermitBlocked(
                    "pending exit cannot settle the current Graph permit outcome"
                )
            permit_patch = {
                "status": permit_settlement,
                "issuerSettledAt": now,
                "updatedAt": SERVER_TIMESTAMP,
            }
            if permit_settlement == "settled_sent":
                exact_sent_evidence = _validate_exact_terminal_sent_evidence(
                    permit,
                    sent_evidence,
                )
                completion_ref, _completion_payload = (
                    _validate_pending_completion_side_document(
                        thread_ref,
                        pending_ref,
                        current,
                        permit,
                        exact_sent_evidence,
                        side_documents,
                    )
                )
                completion_snapshot = completion_ref.get(
                    transaction=transaction
                )
                if completion_snapshot.exists:
                    raise GraphSendPermitBlocked(
                        "pending completion obligation already exists before "
                        "its exact issuer settlement"
                    )
                permit_patch["terminalSentEvidence"] = exact_sent_evidence
            transaction.update(
                capability.permit_ref,
                _stateful_permit_patch(
                    permit,
                    permit_patch,
                    event=f"pending_{permit_settlement}",
                    now=now,
                ),
            )
    elif permit_settlement is not None:
        raise ValueError("permit settlement requires a capability")

    for document_ref, payload in side_documents:
        transaction.set(document_ref, dict(payload))
    if delete_pending:
        transaction.delete(pending_ref)
    else:
        transaction.update(pending_ref, dict(pending_patch or {}))
    transaction.commit()
    return True


def read_permit(capability: GraphSendCapability) -> Dict[str, Any]:
    snapshot = capability.permit_ref.get()
    if not snapshot.exists:
        raise GraphSendPermitError("retained Graph send permit is missing")
    permit = _validate_permit(snapshot.to_dict() or {})
    issuer_document_id, issuer_document_path = _issuer_document_identity(
        capability.issuer_ref,
        issuer_kind=capability.issuer_kind,
    )
    if (
        permit.get("issuerKind") != capability.issuer_kind
        or permit.get("issuerDocumentId") != issuer_document_id
        or permit.get("issuerDocumentPath") != issuer_document_path
    ):
        raise GraphSendPermitError(
            "retained Graph send permit issuer identity/path drifted"
        )
    _validate_settled_draft_review_document(
        permit,
        capability.thread_ref,
    )
    return permit
