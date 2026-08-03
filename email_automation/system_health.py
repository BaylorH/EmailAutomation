from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter

from .clients import _fs
from .send_permits import (
    PENDING_COMPLETION_OBLIGATION_COLLECTION,
    RESOLVED_PENDING_DRAFT_REVIEW_STATUSES,
    RESOLVED_TERMINAL_GRAPH_REVIEW_STATUSES,
    _stable_evidence_hash,
    _unresolved_draft_review_payload,
    validate_pending_completion_obligation_payload,
)


HEALTH_COLLECTION = "systemHealth"
HEALTH_DOC_ID = "emailAutomation"
QUEUE_COLLECTIONS = (
    "outbox",
    "deadLetterQueue",
    "pendingResponses",
    "processingFailures",
    "terminalGraphSendReviews",
    "graphSendDraftReviews",
    PENDING_COMPLETION_OBLIGATION_COLLECTION,
)
TERMINAL_PROTOCOL_HEALTH_KEY = "terminalProtocolThreads"

RESOLVED_DEAD_LETTER_STATUSES = {
    "acknowledged",
    "discarded",
    "reconciled",
    "requeued",
}

# A queue count of this sentinel means the Firestore read failed — the count is
# UNKNOWN, not zero. Health must never treat an unknown count as an empty queue.
COUNT_ERROR = -1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _count_error_severity() -> str:
    """Severity applied when a queue count could not be read.

    Fail-closed by default: absence of config -> "error" (health cannot go green
    while a queue read is failing). Operators may downgrade to "warning" via
    HEALTH_COUNT_ERROR_SEVERITY, but there is deliberately no value that lets an
    unreadable count report "healthy" — that would restore the silent-lie bug.
    """
    raw = str(os.environ.get("HEALTH_COUNT_ERROR_SEVERITY") or "").strip().lower()
    return "warning" if raw == "warning" else "error"


def _count_error_queues(queues: Dict[str, int]) -> List[str]:
    return [name for name, value in queues.items() if isinstance(value, int) and value < 0]


def _count_collection(user_ref, collection_name: str, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection(collection_name)
        query = collection_ref.limit(limit) if hasattr(collection_ref, "limit") else collection_ref
        return len(list(query.stream()))
    except Exception as exc:
        print(f"⚠️ Could not count {collection_name}: {exc}")
        return COUNT_ERROR


def _snapshot_data(snapshot) -> Dict:
    if hasattr(snapshot, "to_dict"):
        return snapshot.to_dict() or {}
    return {}


def _is_resolved_dead_letter(data: Dict) -> bool:
    status = str(data.get("status") or "").strip().lower()
    recovery_status = str(data.get("recoveryStatus") or "").strip().lower()
    return status in RESOLVED_DEAD_LETTER_STATUSES or recovery_status in RESOLVED_DEAD_LETTER_STATUSES


def _count_active_dead_letters(user_ref, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection("deadLetterQueue")
        query = (
            collection_ref.limit(limit + 1)
            if hasattr(collection_ref, "limit")
            else collection_ref
        )
        snapshots = list(query.stream())
        if len(snapshots) > limit:
            print(
                "⚠️ Active dead-letter health scan exceeded its "
                f"fail-closed bound of {limit}"
            )
            return COUNT_ERROR
        return sum(
            1
            for snapshot in snapshots
            if not _is_resolved_dead_letter(_snapshot_data(snapshot))
        )
    except Exception as exc:
        print(f"⚠️ Could not count active deadLetterQueue: {exc}")
        return COUNT_ERROR


def _count_active_terminal_graph_reviews(user_ref, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection("terminalGraphSendReviews")
        query = (
            collection_ref.limit(limit + 1)
            if hasattr(collection_ref, "limit")
            else collection_ref
        )
        snapshots = list(query.stream())
        if len(snapshots) > limit:
            print(
                "⚠️ Terminal Graph review health scan exceeded its "
                f"fail-closed bound of {limit}"
            )
            return COUNT_ERROR
        return sum(
            1
            for snapshot in snapshots
            if str(
                _snapshot_data(snapshot).get("status") or ""
            ).strip().lower() not in RESOLVED_TERMINAL_GRAPH_REVIEW_STATUSES
        )
    except Exception as exc:
        print(f"⚠️ Could not count active terminalGraphSendReviews: {exc}")
        return COUNT_ERROR


def _is_sha256(value) -> bool:
    normalized = str(value or "").strip().lower()
    return bool(
        len(normalized) == 64
        and all(character in "0123456789abcdef" for character in normalized)
    )


def _reference_path(value) -> Optional[str]:
    path = getattr(value, "path", None)
    return path.strip("/") if isinstance(path, str) and path.strip("/") else None


def _same_reference(left, right) -> bool:
    left_path = _reference_path(left)
    right_path = _reference_path(right)
    if left_path is not None or right_path is not None:
        return left_path is not None and left_path == right_path
    return left is right


def _is_aware_datetime(value) -> bool:
    return bool(
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _valid_pending_draft_review_health_record(data: Dict) -> bool:
    status = str(data.get("status") or "").strip().lower()
    if status in RESOLVED_PENDING_DRAFT_REVIEW_STATUSES:
        resolved_at = data.get("resolvedAt")
        original_hash = data.get("originalReviewEvidenceHash")
        original = _unresolved_draft_review_payload(data)
        return bool(
            data.get("resolution") == "retained_draft_not_actionable"
            and data.get("retryAllowed") is True
            and data.get("providerSendStarted") is False
            and data.get("automaticDeleteAttempted") is False
            and _is_sha256(original_hash)
            and str(data.get("operatorSettlementId") or "").strip()
            and str(data.get("resolvedBy") or "").strip()
            and str(data.get("operatorReason") or "").strip()
            and data.get("operatorSettlementAuditRef") is not None
            and _is_aware_datetime(resolved_at)
            and _is_aware_datetime(data.get("updatedAt"))
            and data.get("updatedAt") == resolved_at
            and _valid_pending_draft_review_health_record(original)
            and _stable_evidence_hash(original) == original_hash
        )
    created_at = data.get("createdAt")
    updated_at = data.get("updatedAt")
    return bool(
        status == "manual_review"
        and data.get("source") == "pendingGraphSendProtocol"
        and data.get("authoritative") is True
        and data.get("alreadySent") is False
        and data.get("providerSendStarted") is False
        and data.get("sendOutcomeUnknown") is False
        and data.get("retryAllowed") is False
        and data.get("automaticDeleteAttempted") is False
        and str(data.get("threadId") or "").strip()
        and str(data.get("clientId") or "").strip()
        and str(data.get("pendingDocumentId") or "").strip()
        and str(data.get("graphSendPermitId") or "").startswith(
            "graph-send-"
        )
        and _is_sha256(data.get("graphSendPermitHash"))
        and str(data.get("sourceGraphMessageId") or "").strip()
        and str(data.get("draftMutationState") or "").strip()
        and _is_sha256(data.get("draftResolutionEvidenceHash"))
        and (
            data.get("preparedEnvelopeHash") is None
            or _is_sha256(data.get("preparedEnvelopeHash"))
        )
        and str(data.get("failureReason") or "").strip()
        and _is_aware_datetime(created_at)
        and _is_aware_datetime(updated_at)
        and updated_at >= created_at
    )


def _resolved_pending_draft_review_linkage_is_valid(
    user_ref,
    snapshot,
    data: Dict,
) -> bool:
    user_path = _reference_path(user_ref)
    review_ref = getattr(snapshot, "reference", None)
    review_path = _reference_path(review_ref)
    permit_id = str(data.get("graphSendPermitId") or "").strip()
    thread_id = str(data.get("threadId") or "").strip()
    settlement_id = str(data.get("operatorSettlementId") or "").strip()
    audit_ref = data.get("operatorSettlementAuditRef")
    if (
        user_path is None
        or review_path
        != f"{user_path}/graphSendDraftReviews/pending-{permit_id}"
        or _reference_path(audit_ref)
        != (
            f"{user_path}/graphSendDraftReviewSettlements/"
            f"{settlement_id}"
        )
    ):
        return False
    audit_snapshot = audit_ref.get()
    if getattr(audit_snapshot, "exists", None) is not True:
        return False
    audit = _snapshot_data(audit_snapshot)
    original_hash = data.get("originalReviewEvidenceHash")
    resolved_at = data.get("resolvedAt")
    if (
        audit.get("version") != 1
        or audit.get("settlementId") != settlement_id
        or audit.get("action")
        != "confirm_retained_draft_not_actionable"
        or audit.get("operatorId") != data.get("resolvedBy")
        or audit.get("operatorReason") != data.get("operatorReason")
        or audit.get("threadId") != thread_id
        or audit.get("clientId") != data.get("clientId")
        or audit.get("pendingDocumentId")
        != data.get("pendingDocumentId")
        or audit.get("graphSendPermitId") != permit_id
        or audit.get("graphSendPermitHash")
        != data.get("graphSendPermitHash")
        or audit.get("reviewEvidenceHash") != original_hash
        or not _same_reference(audit.get("reviewEvidenceRef"), review_ref)
        or audit.get("resolution") != "retained_draft_not_actionable"
        or audit.get("providerSendStarted") is not False
        or audit.get("automaticDeleteAttempted") is not False
        or audit.get("retryAllowed") is not True
        or audit.get("resolvedAt") != resolved_at
    ):
        return False
    permit_ref = (
        user_ref.collection("threads").document(thread_id)
        .collection("graphSendPermits").document(permit_id)
    )
    permit_snapshot = permit_ref.get()
    if getattr(permit_snapshot, "exists", None) is not True:
        return False
    permit = _snapshot_data(permit_snapshot)
    review_hash = _stable_evidence_hash(data)
    return bool(
        permit.get("permitId") == permit_id
        and permit.get("immutableHash") == data.get("graphSendPermitHash")
        and permit.get("issuerKind") == "pending_response"
        and permit.get("issuerDocumentId") == data.get("pendingDocumentId")
        and permit.get("threadId") == thread_id
        and permit.get("clientId") == data.get("clientId")
        and permit.get("status") == "settled_draft_review_resolved"
        and permit.get("draftReviewRequired") is False
        and _same_reference(
            permit.get("draftReviewEvidenceRef"),
            review_ref,
        )
        and permit.get("draftReviewEvidenceHash") == review_hash
        and permit.get("pendingReconciliationEvidenceHash") == original_hash
        and _same_reference(
            permit.get("operatorSettlementAuditRef"),
            audit_ref,
        )
        and permit.get("operatorSettlementAuditHash")
        == _stable_evidence_hash(audit)
        and permit.get("operatorOriginalReconciliationEvidenceHash")
        == original_hash
        and permit.get("operatorResolvedReviewEvidenceHash") == review_hash
        and permit.get("operatorResolution")
        == "retained_draft_not_actionable"
    )


def _count_active_pending_draft_reviews(user_ref, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection("graphSendDraftReviews")
        query = (
            collection_ref.limit(limit + 1)
            if hasattr(collection_ref, "limit")
            else collection_ref
        )
        snapshots = list(query.stream())
        if len(snapshots) > limit:
            print(
                "⚠️ Pending draft review health scan exceeded its "
                f"fail-closed bound of {limit}"
            )
            return COUNT_ERROR
        records = [
            (snapshot, _snapshot_data(snapshot))
            for snapshot in snapshots
        ]
        if any(
            not _valid_pending_draft_review_health_record(data)
            or (
                str(data.get("status") or "").strip().lower()
                in RESOLVED_PENDING_DRAFT_REVIEW_STATUSES
                and not _resolved_pending_draft_review_linkage_is_valid(
                    user_ref,
                    snapshot,
                    data,
                )
            )
            for snapshot, data in records
        ):
            print("⚠️ Pending draft review health scan found malformed evidence")
            return COUNT_ERROR
        return sum(
            1
            for _snapshot, data in records
            if str(data.get("status") or "").strip().lower()
            not in RESOLVED_PENDING_DRAFT_REVIEW_STATUSES
        )
    except Exception as exc:
        print(f"⚠️ Could not count active graphSendDraftReviews: {exc}")
        return COUNT_ERROR


def _count_active_pending_completion_obligations(
    user_ref,
    *,
    expected_user_id: str,
    limit: int = 500,
) -> int:
    try:
        collection_ref = user_ref.collection(
            PENDING_COMPLETION_OBLIGATION_COLLECTION
        )
        snapshots = list(
            collection_ref.where(
                filter=FieldFilter("status", "==", "owed")
            ).limit(limit + 1).stream()
        )
        if len(snapshots) > limit:
            print(
                "⚠️ Pending completion obligation health scan exceeded its "
                f"fail-closed bound of {limit}"
            )
            return COUNT_ERROR
        for snapshot in snapshots:
            validate_pending_completion_obligation_payload(
                _snapshot_data(snapshot),
                document_id=str(getattr(snapshot, "id", None) or ""),
                expected_user_id=expected_user_id,
            )
        return len(snapshots)
    except Exception as exc:
        print(
            "⚠️ Could not count active "
            f"{PENDING_COMPLETION_OBLIGATION_COLLECTION}: {exc}"
        )
        return COUNT_ERROR


def _has_active_terminal_protocol(data: Dict) -> bool:
    attempt = data.get("terminalReplyAttempt")
    attempt_status = (
        str(attempt.get("status") or "").strip().lower()
        if isinstance(attempt, dict)
        else ""
    )
    return bool(
        data.get("terminalReplyOwed")
        or data.get("terminalNotificationOwed")
        or data.get("terminalSagaKey")
        or data.get("pendingTerminalReason")
        or isinstance(data.get("terminalSaga"), dict)
        or isinstance(data.get("terminalSagaClaim"), dict)
        or (
            isinstance(attempt, dict)
            and attempt_status not in {"committed", "reconciled"}
        )
    )


def _count_active_terminal_protocol_threads(user_ref, limit: int = 500) -> int:
    try:
        collection_ref = user_ref.collection("threads")
        query = (
            collection_ref.limit(limit + 1)
            if hasattr(collection_ref, "limit")
            else collection_ref
        )
        snapshots = list(query.stream())
        if len(snapshots) > limit:
            print(
                "⚠️ Terminal protocol thread health scan exceeded its "
                f"fail-closed bound of {limit}"
            )
            return COUNT_ERROR
        return sum(
            1
            for snapshot in snapshots
            if _has_active_terminal_protocol(_snapshot_data(snapshot))
        )
    except Exception as exc:
        print(f"⚠️ Could not count active terminal protocol threads: {exc}")
        return COUNT_ERROR


def _overall_status(token_state: Dict, graph_state: Dict, queues: Dict[str, int]) -> str:
    if token_state.get("status") == "error" or graph_state.get("status") == "error":
        return "error"
    # Fail closed: a queue we could not read (COUNT_ERROR sentinel) is an UNKNOWN
    # backlog, not an empty one. It must never be treated as healthy — a Firestore
    # read outage could be hiding a growing dead-letter / pending backlog of stuck
    # or misdirected sends. Default severity is "error"; operators may downgrade to
    # "warning" via HEALTH_COUNT_ERROR_SEVERITY but never to "healthy".
    if _count_error_queues(queues):
        return _count_error_severity()
    if any(value > 0 for value in queues.values()):
        return "warning"
    if token_state.get("status") == "unknown" or graph_state.get("status") == "unknown":
        return "warning"
    return "healthy"


def collect_user_health(
    user_id: str,
    *,
    fs_client=None,
    token_state: Optional[Dict] = None,
    graph_state: Optional[Dict] = None,
    now: Optional[datetime] = None,
) -> Dict:
    fs_client = fs_client or _fs
    token_state = token_state or {"status": "unknown"}
    graph_state = graph_state or {"status": "unknown"}
    now = now or _utc_now()
    user_ref = fs_client.collection("users").document(user_id)
    queues = {
        name: (
            _count_active_dead_letters(user_ref)
            if name == "deadLetterQueue"
            else _count_active_terminal_graph_reviews(user_ref)
            if name == "terminalGraphSendReviews"
            else _count_active_pending_draft_reviews(user_ref)
            if name == "graphSendDraftReviews"
            else _count_active_pending_completion_obligations(
                user_ref,
                expected_user_id=user_id,
            )
            if name == PENDING_COMPLETION_OBLIGATION_COLLECTION
            else _count_collection(user_ref, name)
        )
        for name in QUEUE_COLLECTIONS
    }
    queues[TERMINAL_PROTOCOL_HEALTH_KEY] = (
        _count_active_terminal_protocol_threads(user_ref)
    )

    return {
        "status": _overall_status(token_state, graph_state, queues),
        "token": token_state,
        "graph": graph_state,
        "queues": queues,
        "countErrors": _count_error_queues(queues),
        "lastCheckedAt": now,
        "updatedAt": SERVER_TIMESTAMP,
    }


def write_user_health(user_id: str, payload: Dict, *, fs_client=None) -> None:
    fs_client = fs_client or _fs
    (
        fs_client.collection("users").document(user_id)
        .collection(HEALTH_COLLECTION).document(HEALTH_DOC_ID)
        .set(payload)
    )


def record_user_health(
    user_id: str,
    *,
    fs_client=None,
    token_state: Optional[Dict] = None,
    graph_state: Optional[Dict] = None,
) -> Dict:
    payload = collect_user_health(
        user_id,
        fs_client=fs_client,
        token_state=token_state,
        graph_state=graph_state,
    )
    write_user_health(user_id, payload, fs_client=fs_client)
    return payload
