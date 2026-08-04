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
from .source_coordinator import (
    SourceAliasConflict,
    SourceCoordinatorError,
    SourceSettlementConflict,
    SourceSettlementNotReady,
    _PROCESSED_ALIAS_FIELDS,
    _SOURCE_ALIAS_PROJECTION_FIELDS,
    _SOURCE_ALIAS_TYPES,
    _SOURCE_SETTLEMENT_FIELDS,
    _blocker_from_head,
    _source_deferred_work_immutable_material,
    _thread_head_hash_material,
    _validate_blocker,
    _validate_admission_authority_bindings,
    _validate_alias_projection,
    _validate_blocked_projection_document,
    _validate_classification_document,
    _validate_pending_admission_document,
    _validate_processed_alias_projection,
    _validate_source_deferred_work_document,
    _validate_source_resume_bindings,
    _validate_source_settlement_document,
    _validate_source_work_ledger_document,
    _validate_thread_head_document,
    _validate_transition_owner_document,
    _validated_identity_descriptors,
    _wake_token_for_release,
    canonical_json_hash,
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
HEALTH_SCAN_LIMIT = 500
B1_HEALTH_KEYS = (
    "b1ActiveClassifications",
    "b1AmbiguousClassifications",
    "b1BlockedSources",
    "b1NonsettledPendingAdmissions",
    "b1UnsettledWorkLedgers",
    "b1AliasConflicts",
    "b1MarkerOrSettlementAmbiguities",
    "b1LegacyTerminalQuarantined",
    "b1LegacyMarkerOnlyAmbiguous",
    "b1LegacyReplayClaimQuarantined",
)
B1_COLLECTIONS = (
    "sourceIdentities",
    "sourceAliases",
    "sourceClassifications",
    "sourceTransitionOwners",
    "threadTransitionHeads",
    "sourceWorkLedgers",
    "sourceDeferredWork",
    "inboundPendingAdmissions",
    "blockedSources",
    "sourceSettlements",
)
B1_SCAN_COLLECTIONS = (*B1_COLLECTIONS, "processedMessages")
_B1_COLLECTION_ERROR_KEYS = {
    "sourceIdentities": {
        "b1ActiveClassifications",
        "b1AmbiguousClassifications",
        "b1NonsettledPendingAdmissions",
        "b1UnsettledWorkLedgers",
        "b1AliasConflicts",
        "b1MarkerOrSettlementAmbiguities",
        "b1LegacyTerminalQuarantined",
    },
    "sourceAliases": {
        "b1AliasConflicts",
        "b1MarkerOrSettlementAmbiguities",
    },
    "sourceClassifications": {
        "b1ActiveClassifications",
        "b1AmbiguousClassifications",
        "b1NonsettledPendingAdmissions",
        "b1UnsettledWorkLedgers",
        "b1MarkerOrSettlementAmbiguities",
        "b1LegacyTerminalQuarantined",
    },
    "sourceTransitionOwners": {
        "b1NonsettledPendingAdmissions",
        "b1UnsettledWorkLedgers",
        "b1MarkerOrSettlementAmbiguities",
    },
    "threadTransitionHeads": {
        "b1BlockedSources",
        "b1NonsettledPendingAdmissions",
    },
    "sourceWorkLedgers": {
        "b1NonsettledPendingAdmissions",
        "b1UnsettledWorkLedgers",
        "b1MarkerOrSettlementAmbiguities",
    },
    "sourceDeferredWork": {"b1UnsettledWorkLedgers"},
    "inboundPendingAdmissions": {
        "b1BlockedSources",
        "b1NonsettledPendingAdmissions",
        "b1MarkerOrSettlementAmbiguities",
    },
    "blockedSources": {"b1BlockedSources"},
    "sourceSettlements": {"b1MarkerOrSettlementAmbiguities"},
    "processedMessages": {
        "b1MarkerOrSettlementAmbiguities",
        "b1LegacyMarkerOnlyAmbiguous",
        "b1LegacyReplayClaimQuarantined",
    },
}
_B1_PROCESSED_OWNERSHIP_FIELDS = {
    "canonicalSourceId",
    "settlementRevision",
    "settlementHash",
    "sourceAliasKey",
}
_B1_ADMISSION_ERROR_KEYS = {
    "b1BlockedSources",
    "b1NonsettledPendingAdmissions",
    "b1MarkerOrSettlementAmbiguities",
}


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


def _scan_b1_collection(user_ref, collection_name: str):
    """Return an exact, bounded read-only snapshot for one B1 collection."""
    try:
        snapshots = list(
            user_ref.collection(collection_name)
            .limit(HEALTH_SCAN_LIMIT + 1)
            .stream()
        )
        if len(snapshots) > HEALTH_SCAN_LIMIT:
            raise ValueError("bounded B1 health scan overflow")
        documents = {}
        for snapshot in snapshots:
            document_id = getattr(snapshot, "id", None)
            data = snapshot.to_dict()
            if (
                type(document_id) is not str
                or not document_id
                or type(data) is not dict
                or document_id in documents
            ):
                raise ValueError("unreadable B1 health document")
            documents[document_id] = data
        return documents, True
    except Exception:
        # Collection names are static source literals. Never render exception
        # text, document identifiers, or stored authority data in health logs.
        print(f"⚠️ B1 health scan failed closed for {collection_name}")
        return {}, False


def _mark_b1_validation_error(error_keys, affected_keys, validation_log_state):
    error_keys.update(affected_keys)
    if not validation_log_state[0]:
        print("⚠️ B1 health authority validation failed closed")
        validation_log_state[0] = True


def _is_schema_v1(value) -> bool:
    return type(value) is int and value == 1


def _valid_settlement_shape(data: Dict, canonical_source_id: str) -> bool:
    if (
        type(data) is not dict
        or set(data) != _SOURCE_SETTLEMENT_FIELDS
        or not _is_schema_v1(data.get("schemaVersion"))
        or data.get("canonicalSourceId") != canonical_source_id
        or type(data.get("settlementRevision")) is not int
        or data.get("settlementRevision") != 1
        or any(
            not _is_sha256(data.get(field))
            for field in (
                "identityHash",
                "snapshotImmutableHash",
                "selectionHash",
                "ownerDecisionHash",
                "ledgerHash",
                "finalLedgerEvidenceHash",
                "aliasSetHash",
                "settlementHash",
            )
        )
        or not _is_aware_datetime(data.get("settledAt"))
    ):
        return False
    aliases = data.get("aliases")
    if type(aliases) is not list or not aliases:
        return False
    thread_head_binding = data.get("threadHeadBinding")
    if thread_head_binding is not None:
        try:
            _validate_blocker(thread_head_binding)
        except SourceCoordinatorError:
            return False
    seen = set()
    for descriptor in aliases:
        if (
            type(descriptor) is not dict
            or set(descriptor)
            != {"sourceAliasKey", "aliasType", "normalizedValueHash"}
            or not _is_sha256(descriptor.get("sourceAliasKey"))
            or descriptor.get("aliasType") not in _SOURCE_ALIAS_TYPES
            or not _is_sha256(descriptor.get("normalizedValueHash"))
            or descriptor["sourceAliasKey"] in seen
        ):
            return False
        seen.add(descriptor["sourceAliasKey"])
    return aliases == sorted(aliases, key=lambda item: item["sourceAliasKey"])


def _valid_alias_projection_shape(document_id: str, data: Dict) -> bool:
    return bool(
        type(data) is dict
        and set(data) == _SOURCE_ALIAS_PROJECTION_FIELDS
        and _is_schema_v1(data.get("schemaVersion"))
        and data.get("sourceAliasKey") == document_id
        and data.get("aliasType") in _SOURCE_ALIAS_TYPES
        and _is_sha256(data.get("sourceAliasKey"))
        and _is_sha256(data.get("normalizedValueHash"))
        and type(data.get("canonicalSourceId")) is str
        and bool(data.get("canonicalSourceId"))
        and _is_aware_datetime(data.get("createdAt"))
    )


def _valid_processed_projection_shape(
    document_id: str,
    data: Dict,
) -> bool:
    return bool(
        type(data) is dict
        and set(data) == _PROCESSED_ALIAS_FIELDS
        and _is_schema_v1(data.get("schemaVersion"))
        and data.get("sourceAliasKey") == document_id
        and data.get("aliasType") in _SOURCE_ALIAS_TYPES
        and _is_sha256(data.get("sourceAliasKey"))
        and _is_sha256(data.get("normalizedValueHash"))
        and type(data.get("canonicalSourceId")) is str
        and bool(data.get("canonicalSourceId"))
        and type(data.get("settlementRevision")) is int
        and data.get("settlementRevision") == 1
        and _is_sha256(data.get("settlementHash"))
        and _is_aware_datetime(data.get("processedAt"))
    )


def _prior_active_blocker_for_releasing_head(
    head_data: Dict,
    *,
    thread_id: str,
) -> Dict:
    prior_head = {
        "schemaVersion": head_data["schemaVersion"],
        "threadId": thread_id,
        "threadHeadRevision": head_data["threadHeadRevision"] - 1,
        "activeOwnerKey": head_data["activeOwnerKey"],
        "activeOwnerKind": head_data["activeOwnerKind"],
        "activeCanonicalSourceId": head_data["activeCanonicalSourceId"],
        "activeGeneration": head_data["activeGeneration"],
        "activeState": "active",
    }
    prior_head["headHash"] = canonical_json_hash(
        _thread_head_hash_material(prior_head)
    )
    return _blocker_from_head(prior_head)


def _expected_current_blocker(head_data: Dict, *, thread_id: str) -> Dict:
    if head_data["activeState"] == "active":
        return _blocker_from_head(head_data)
    if head_data["activeState"] == "releasing":
        return _prior_active_blocker_for_releasing_head(
            head_data,
            thread_id=thread_id,
        )
    raise SourceCoordinatorError("clear thread head cannot retain a blocker")


def _thread_head_revision_matches_generation(head_data: Dict) -> bool:
    expected_revision = 2 * head_data["activeGeneration"]
    if head_data["activeState"] == "active":
        expected_revision -= 1
    return head_data["threadHeadRevision"] == expected_revision


def _validate_admission_head_relationship(
    admission: Dict,
    *,
    head_data: Optional[Dict],
    thread_id: str,
    user_id: str,
) -> None:
    state = admission["admissionState"]
    lifecycle = admission["blockedLifecycleState"]
    owner_kind = admission["ownerKind"]
    current_blocker = admission["currentBlocker"]

    if state == "pending":
        if owner_kind != "none" and (
            head_data is not None and head_data["activeState"] != "clear"
        ):
            raise SourceCoordinatorError(
                "pending transition admission conflicts with occupied head"
            )
        return
    if state == "settled":
        return
    if owner_kind == "none" or head_data is None:
        raise SourceCoordinatorError(
            "transition admission lacks retained thread-head authority"
        )

    if state == "blocked":
        if lifecycle not in {"blocked", "eligible"}:
            raise SourceCoordinatorError(
                "blocked admission lifecycle has no production writer"
            )
        if head_data["activeState"] not in {"active", "releasing"}:
            raise SourceCoordinatorError(
                "blocked admission lacks an actionable thread head"
            )
        expected_blocker = _expected_current_blocker(
            head_data,
            thread_id=thread_id,
        )
        if current_blocker != expected_blocker:
            raise SourceCoordinatorError(
                "blocked admission conflicts with thread-head blocker"
            )
        if lifecycle == "blocked":
            if admission["wakeState"] != "none":
                raise SourceCoordinatorError(
                    "blocked admission retains an unexpected wake"
                )
            return
        if (
            head_data["activeState"] != "releasing"
            or admission["wakeState"] != "eligible"
            or admission["wakeGeneration"]
            != head_data["activeGeneration"] + 1
            or admission["wakeToken"]
            != _wake_token_for_release(
                user_id=user_id,
                thread_id=thread_id,
                admission=admission,
                released_blocker=current_blocker,
                wake_generation=admission["wakeGeneration"],
            )
        ):
            raise SourceCoordinatorError(
                "eligible wake conflicts with releasing thread head"
            )
        return

    if state == "processing":
        if (
            head_data["activeState"] != "active"
            or head_data["activeCanonicalSourceId"]
            != admission["canonicalSourceId"]
            or head_data["activeOwnerKind"] != owner_kind
            or head_data["activeOwnerKey"] != admission["ownerKey"]
        ):
            raise SourceCoordinatorError(
                "processing admission conflicts with active thread head"
            )
        if lifecycle is None:
            return
        if (
            lifecycle != "settled_as_new_blocker"
            or admission["wakeState"] != "consumed"
            or head_data["activeGeneration"] != admission["wakeGeneration"]
            or current_blocker is None
            or current_blocker["generation"] + 1
            != admission["wakeGeneration"]
            or admission["wakeToken"]
            != _wake_token_for_release(
                user_id=user_id,
                thread_id=thread_id,
                admission=admission,
                released_blocker=current_blocker,
                wake_generation=admission["wakeGeneration"],
            )
        ):
            raise SourceCoordinatorError(
                "consumed wake conflicts with rebound thread head"
            )
        return

    raise SourceCoordinatorError("admission state lacks head semantics")


def _settled_owned_head_outcome_is_valid(
    admission: Dict,
    *,
    owner_data: Dict,
    settlement_data: Dict,
    head_data: Optional[Dict],
) -> bool:
    if head_data is None:
        return False
    canonical_source_id = admission["canonicalSourceId"]
    retained_binding = settlement_data["threadHeadBinding"]
    if head_data["activeCanonicalSourceId"] == canonical_source_id:
        if (
            head_data["activeState"] not in {"active", "releasing"}
            or head_data["activeOwnerKind"] != owner_data["ownerKind"]
            or head_data["activeOwnerKey"] != owner_data["ownerKey"]
        ):
            return False
        if head_data["activeState"] == "active":
            retained_head = _blocker_from_head(head_data)
        else:
            retained_head = _prior_active_blocker_for_releasing_head(
                head_data,
                thread_id=admission["threadId"],
            )
        return retained_head == retained_binding
    return bool(
        head_data["activeGeneration"] >= retained_binding["generation"]
        and head_data["threadHeadRevision"]
        > retained_binding["threadHeadRevision"]
        and head_data["updatedAt"] >= settlement_data["settledAt"]
        and (
            head_data["activeState"] == "clear"
            or head_data["activeGeneration"]
            > retained_binding["generation"]
        )
    )


def _retained_blocker_lineage_index(admissions: Dict[str, Dict]):
    blocker_events = {}
    generation_owners = {}
    for canonical_source_id, admission in admissions.items():
        thread_id = admission["threadId"]
        if admission["wakeState"] == "consumed":
            generation = admission["wakeGeneration"]
            generation_key = (thread_id, generation)
            existing_owner = generation_owners.get(generation_key)
            if (
                existing_owner is not None
                and existing_owner != canonical_source_id
            ):
                return None
            generation_owners[generation_key] = canonical_source_id
        for field in ("initialBlocker", "currentBlocker"):
            blocker = admission[field]
            if blocker is None:
                continue
            if (
                blocker["threadHeadRevision"]
                != (2 * blocker["generation"]) - 1
            ):
                return None
            predecessor_id = blocker["canonicalSourceId"]
            predecessor_key = (thread_id, predecessor_id)
            existing_event = blocker_events.get(predecessor_key)
            if existing_event is not None and existing_event != blocker:
                return None
            blocker_events[predecessor_key] = blocker

    for (thread_id, predecessor_id), blocker in blocker_events.items():
        predecessor_admission = admissions.get(predecessor_id)
        if (
            predecessor_admission is None
            or predecessor_admission["threadId"] != thread_id
        ):
            return None
        generation = blocker["generation"]
        generation_key = (thread_id, generation)
        existing_owner = generation_owners.get(generation_key)
        if existing_owner is not None and existing_owner != predecessor_id:
            return None
        generation_owners[generation_key] = predecessor_id
        if (
            predecessor_admission["wakeState"] == "consumed"
            and predecessor_admission["wakeGeneration"] != generation
        ):
            return None
    return blocker_events, generation_owners


def _consumed_wake_lineage_is_valid(
    admission: Dict,
    *,
    admissions: Dict[str, Dict],
    blocker_events: Dict[tuple[str, str], Dict],
    generation_owners: Dict[tuple[str, int], str],
    owners: Dict[str, Dict],
    settlements: Dict[str, Dict],
    head_data: Optional[Dict],
    thread_id: str,
    user_id: str,
) -> bool:
    if admission["wakeState"] != "consumed":
        return True
    blocker = admission["currentBlocker"]
    wake_generation = admission["wakeGeneration"]
    if (
        admission["blockedLifecycleState"] != "settled_as_new_blocker"
        or blocker is None
        or type(wake_generation) is not int
        or blocker["generation"] + 1 != wake_generation
        or head_data is None
        or head_data["threadHeadRevision"]
        < blocker["threadHeadRevision"] + 2
        or head_data["activeGeneration"] < wake_generation
    ):
        return False

    predecessor_id = blocker["canonicalSourceId"]
    predecessor_admission = admissions.get(predecessor_id)
    predecessor_owner = owners.get(predecessor_id)
    predecessor_settlement = settlements.get(predecessor_id)
    predecessor_event = blocker_events.get((thread_id, predecessor_id))
    generation_owner = generation_owners.get(
        (thread_id, blocker["generation"])
    )
    if (
        predecessor_id == admission["canonicalSourceId"]
        or predecessor_admission is None
        or predecessor_owner is None
        or predecessor_settlement is None
        or predecessor_settlement["threadHeadBinding"] != blocker
        or predecessor_event != blocker
        or generation_owner != predecessor_id
        or predecessor_admission["threadId"] != thread_id
        or predecessor_admission["admissionState"] != "settled"
        or predecessor_owner["ownerKind"] != blocker["ownerKind"]
        or predecessor_owner["ownerKey"] != blocker["ownerKey"]
    ):
        return False

    prior_active_head = {
        "schemaVersion": head_data["schemaVersion"],
        "threadId": thread_id,
        "threadHeadRevision": blocker["threadHeadRevision"],
        "activeOwnerKey": blocker["ownerKey"],
        "activeOwnerKind": blocker["ownerKind"],
        "activeCanonicalSourceId": predecessor_id,
        "activeGeneration": blocker["generation"],
        "activeState": "active",
    }
    if blocker["headHash"] != canonical_json_hash(
        _thread_head_hash_material(prior_active_head)
    ):
        return False
    if admission["wakeToken"] != _wake_token_for_release(
        user_id=user_id,
        thread_id=thread_id,
        admission=admission,
        released_blocker=blocker,
        wake_generation=wake_generation,
    ):
        return False
    if (
        admission["admissionState"] == "settled"
        and admission["canonicalSourceId"] not in settlements
    ):
        return False
    return True


def _collect_b1_health_counts(user_ref, *, user_id: str) -> Dict[str, int]:
    counts = {key: 0 for key in B1_HEALTH_KEYS}
    error_keys = set()
    validation_log_state = [False]
    scans = {}
    readable = {}
    for collection_name in B1_SCAN_COLLECTIONS:
        scans[collection_name], readable[collection_name] = (
            _scan_b1_collection(user_ref, collection_name)
        )
        if not readable[collection_name]:
            error_keys.update(_B1_COLLECTION_ERROR_KEYS[collection_name])

    valid_identities = {}
    descriptors_by_source = {}
    if readable["sourceIdentities"]:
        for canonical_source_id, data in scans["sourceIdentities"].items():
            try:
                descriptors = _validated_identity_descriptors(
                    data,
                    canonical_source_id=canonical_source_id,
                )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceIdentities"],
                    validation_log_state,
                )
                continue
            valid_identities[canonical_source_id] = data
            descriptors_by_source[canonical_source_id] = descriptors

    alias_conflicts = set()
    expected_aliases = {}
    for canonical_source_id, descriptors in descriptors_by_source.items():
        for descriptor in descriptors:
            expected_aliases.setdefault(descriptor["sourceAliasKey"], []).append(
                (canonical_source_id, descriptor)
            )
    if readable["sourceAliases"]:
        alias_documents = scans["sourceAliases"]
        for source_alias_key, owners in expected_aliases.items():
            if len(owners) != 1:
                alias_conflicts.add(source_alias_key)
            alias_data = alias_documents.get(source_alias_key)
            if alias_data is None:
                _mark_b1_validation_error(
                    error_keys,
                    {"b1AliasConflicts", "b1MarkerOrSettlementAmbiguities"},
                    validation_log_state,
                )
                continue
            if not _valid_alias_projection_shape(
                source_alias_key,
                alias_data,
            ):
                _mark_b1_validation_error(
                    error_keys,
                    {"b1AliasConflicts", "b1MarkerOrSettlementAmbiguities"},
                    validation_log_state,
                )
                continue
            canonical_source_id, descriptor = owners[0]
            try:
                _validate_alias_projection(
                    alias_data,
                    descriptor=descriptor,
                    canonical_source_id=canonical_source_id,
                )
            except SourceAliasConflict:
                alias_conflicts.add(source_alias_key)
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    {"b1AliasConflicts", "b1MarkerOrSettlementAmbiguities"},
                    validation_log_state,
                )
        for source_alias_key in alias_documents:
            if source_alias_key not in expected_aliases:
                _mark_b1_validation_error(
                    error_keys,
                    {"b1AliasConflicts", "b1MarkerOrSettlementAmbiguities"},
                    validation_log_state,
                )
    counts["b1AliasConflicts"] = len(alias_conflicts)

    valid_classifications = {}
    if readable["sourceClassifications"]:
        for canonical_source_id, data in scans["sourceClassifications"].items():
            if canonical_source_id not in valid_identities:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceClassifications"],
                    validation_log_state,
                )
                continue
            try:
                _validate_classification_document(
                    data,
                    canonical_source_id=canonical_source_id,
                )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceClassifications"],
                    validation_log_state,
                )
                continue
            valid_classifications[canonical_source_id] = data
            state = data["classificationState"]
            if state in {"claimed", "request_started"}:
                counts["b1ActiveClassifications"] += 1
            elif state == "classification_request_ambiguous":
                counts["b1AmbiguousClassifications"] += 1
            elif state == "legacy_terminal_quarantined":
                counts["b1LegacyTerminalQuarantined"] += 1
        for canonical_source_id in valid_identities:
            if canonical_source_id not in valid_classifications:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceClassifications"],
                    validation_log_state,
                )

    valid_owners = {}
    if readable["sourceTransitionOwners"]:
        for canonical_source_id, data in scans["sourceTransitionOwners"].items():
            classification_data = valid_classifications.get(canonical_source_id)
            if (
                canonical_source_id not in valid_identities
                or classification_data is None
            ):
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceTransitionOwners"],
                    validation_log_state,
                )
                continue
            try:
                _validate_transition_owner_document(
                    data,
                    canonical_source_id=canonical_source_id,
                    classification_data=classification_data,
                )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceTransitionOwners"],
                    validation_log_state,
                )
                continue
            valid_owners[canonical_source_id] = data
        for canonical_source_id, classification_data in (
            valid_classifications.items()
        ):
            if (
                classification_data["classificationState"] == "snapshot_ready"
                and canonical_source_id not in valid_owners
            ):
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceTransitionOwners"],
                    validation_log_state,
                )

    valid_ledgers = {}
    work_entries = {}
    delegated_work_keys = set()
    if readable["sourceWorkLedgers"]:
        for canonical_source_id, data in scans["sourceWorkLedgers"].items():
            classification_data = valid_classifications.get(canonical_source_id)
            owner_data = valid_owners.get(canonical_source_id)
            if (
                canonical_source_id not in valid_identities
                or classification_data is None
                or owner_data is None
            ):
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceWorkLedgers"],
                    validation_log_state,
                )
                continue
            try:
                _validate_source_work_ledger_document(
                    data,
                    canonical_source_id=canonical_source_id,
                    classification_data=classification_data,
                    owner_data=owner_data,
                )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceWorkLedgers"],
                    validation_log_state,
                )
                continue
            valid_ledgers[canonical_source_id] = data
            if any(
                entry["state"] in {"pending", "applying"}
                for entry in data["entries"]
            ):
                counts["b1UnsettledWorkLedgers"] += 1
            for entry in data["entries"]:
                work_key = entry["workKey"]
                if work_key in work_entries:
                    _mark_b1_validation_error(
                        error_keys,
                        {"b1UnsettledWorkLedgers"},
                        validation_log_state,
                    )
                    continue
                work_entries[work_key] = (
                    canonical_source_id,
                    data,
                    entry,
                )
                if entry["state"] == "delegated":
                    delegated_work_keys.add(work_key)
        for canonical_source_id in valid_owners:
            if canonical_source_id not in valid_ledgers:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceWorkLedgers"],
                    validation_log_state,
                )

    if readable["sourceDeferredWork"]:
        deferred_documents = scans["sourceDeferredWork"]
        for work_key, data in deferred_documents.items():
            entry_bundle = work_entries.get(work_key)
            if entry_bundle is None or work_key not in delegated_work_keys:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceDeferredWork"],
                    validation_log_state,
                )
                continue
            canonical_source_id, ledger_data, entry = entry_bundle
            try:
                expected = _source_deferred_work_immutable_material(
                    canonical_source_id=canonical_source_id,
                    ledger_hash=ledger_data["ledgerHash"],
                    entry=entry,
                )
                _validate_source_deferred_work_document(
                    data,
                    expected_immutable=expected,
                )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceDeferredWork"],
                    validation_log_state,
                )
        if any(
            work_key not in deferred_documents
            for work_key in delegated_work_keys
        ):
            _mark_b1_validation_error(
                error_keys,
                _B1_COLLECTION_ERROR_KEYS["sourceDeferredWork"],
                validation_log_state,
            )

    valid_heads = {}
    if readable["threadTransitionHeads"]:
        for thread_id, data in scans["threadTransitionHeads"].items():
            try:
                _validate_thread_head_document(data, thread_id=thread_id)
                if not _thread_head_revision_matches_generation(data):
                    raise SourceCoordinatorError(
                        "thread head revision conflicts with its generation"
                    )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["threadTransitionHeads"],
                    validation_log_state,
                )
                continue
            valid_heads[thread_id] = data

    valid_admissions = {}
    if readable["inboundPendingAdmissions"]:
        for canonical_source_id, data in scans[
            "inboundPendingAdmissions"
        ].items():
            identity_data = valid_identities.get(canonical_source_id)
            classification_data = valid_classifications.get(canonical_source_id)
            owner_data = valid_owners.get(canonical_source_id)
            ledger_data = valid_ledgers.get(canonical_source_id)
            thread_id = data.get("threadId")
            if (
                identity_data is None
                or classification_data is None
                or owner_data is None
                or ledger_data is None
                or type(thread_id) is not str
                or not thread_id
                or identity_data.get("threadId") != thread_id
            ):
                _mark_b1_validation_error(
                    error_keys,
                    _B1_ADMISSION_ERROR_KEYS,
                    validation_log_state,
                )
                continue
            try:
                _validate_pending_admission_document(
                    data,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                )
                _validate_source_resume_bindings(
                    data,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                )
                _validate_admission_authority_bindings(
                    data,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                )
                _validate_admission_head_relationship(
                    data,
                    head_data=valid_heads.get(thread_id),
                    thread_id=thread_id,
                    user_id=user_id,
                )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_ADMISSION_ERROR_KEYS,
                    validation_log_state,
                )
                continue
            valid_admissions[canonical_source_id] = data
            if data["admissionState"] in {"pending", "blocked", "processing"}:
                counts["b1NonsettledPendingAdmissions"] += 1
        for canonical_source_id in valid_ledgers:
            if canonical_source_id not in valid_admissions:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["inboundPendingAdmissions"],
                    validation_log_state,
                )

    for thread_id, head_data in valid_heads.items():
        if head_data["activeState"] == "clear":
            continue
        active_source_id = head_data["activeCanonicalSourceId"]
        admission = valid_admissions.get(active_source_id)
        if (
            admission is None
            or admission["threadId"] != thread_id
            or admission["ownerKind"] != head_data["activeOwnerKind"]
            or admission["ownerKey"] != head_data["activeOwnerKey"]
            or admission["admissionState"] not in {"processing", "settled"}
            or (
                head_data["activeState"] == "releasing"
                and admission["admissionState"] != "settled"
            )
        ):
            _mark_b1_validation_error(
                error_keys,
                _B1_COLLECTION_ERROR_KEYS["threadTransitionHeads"],
                validation_log_state,
            )
        if head_data["activeState"] == "releasing":
            eligible = [
                candidate
                for candidate in valid_admissions.values()
                if candidate["threadId"] == thread_id
                and candidate["admissionState"] == "blocked"
                and candidate["blockedLifecycleState"] == "eligible"
                and candidate["wakeState"] == "eligible"
            ]
            if len(eligible) != 1:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["threadTransitionHeads"],
                    validation_log_state,
                )

    valid_blocked_projection_ids = set()
    if readable["blockedSources"]:
        for canonical_source_id, data in scans["blockedSources"].items():
            admission = valid_admissions.get(canonical_source_id)
            if admission is None:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["blockedSources"],
                    validation_log_state,
                )
                continue
            try:
                _validate_blocked_projection_document(
                    data,
                    admission=admission,
                )
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["blockedSources"],
                    validation_log_state,
                )
                continue
            valid_blocked_projection_ids.add(canonical_source_id)
            if admission["admissionState"] != "settled":
                counts["b1BlockedSources"] += 1
        if any(
            admission["currentBlocker"] is not None
            and canonical_source_id not in valid_blocked_projection_ids
            for canonical_source_id, admission in valid_admissions.items()
        ):
            _mark_b1_validation_error(
                error_keys,
                _B1_COLLECTION_ERROR_KEYS["blockedSources"],
                validation_log_state,
            )

    ambiguity_tokens = set()
    structurally_valid_settlements = {}
    fully_valid_settlements = {}
    if readable["sourceSettlements"]:
        for canonical_source_id, data in scans["sourceSettlements"].items():
            if not _valid_settlement_shape(data, canonical_source_id):
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceSettlements"],
                    validation_log_state,
                )
                continue
            structurally_valid_settlements[canonical_source_id] = data
            identity_data = valid_identities.get(canonical_source_id)
            classification_data = valid_classifications.get(canonical_source_id)
            owner_data = valid_owners.get(canonical_source_id)
            ledger_data = valid_ledgers.get(canonical_source_id)
            admission_data = valid_admissions.get(canonical_source_id)
            if any(
                value is None
                for value in (
                    identity_data,
                    classification_data,
                    owner_data,
                    ledger_data,
                    admission_data,
                )
            ):
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceSettlements"],
                    validation_log_state,
                )
                continue
            try:
                _validate_source_settlement_document(
                    data,
                    canonical_source_id=canonical_source_id,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                    current_aliases=descriptors_by_source[canonical_source_id],
                )
            except (SourceSettlementConflict, SourceSettlementNotReady):
                ambiguity_tokens.add(("settlement", canonical_source_id))
            except Exception:
                _mark_b1_validation_error(
                    error_keys,
                    _B1_COLLECTION_ERROR_KEYS["sourceSettlements"],
                    validation_log_state,
                )
            else:
                if (
                    owner_data["ownerKind"] != "none"
                    and not _settled_owned_head_outcome_is_valid(
                        admission_data,
                        owner_data=owner_data,
                        settlement_data=data,
                        head_data=valid_heads.get(admission_data["threadId"]),
                    )
                ):
                    _mark_b1_validation_error(
                        error_keys,
                        {"b1MarkerOrSettlementAmbiguities"},
                        validation_log_state,
                    )
                else:
                    fully_valid_settlements[canonical_source_id] = data
            if admission_data["admissionState"] != "settled":
                ambiguity_tokens.add(("settlement", canonical_source_id))
        for canonical_source_id, admission in valid_admissions.items():
            if (
                admission["admissionState"] == "settled"
                and canonical_source_id
                not in structurally_valid_settlements
            ):
                ambiguity_tokens.add(("settlement", canonical_source_id))

    blocker_lineage = _retained_blocker_lineage_index(valid_admissions)
    if blocker_lineage is None:
        _mark_b1_validation_error(
            error_keys,
            _B1_ADMISSION_ERROR_KEYS,
            validation_log_state,
        )
        blocker_events, generation_owners = {}, {}
    else:
        blocker_events, generation_owners = blocker_lineage
    for admission in valid_admissions.values():
        if not _consumed_wake_lineage_is_valid(
            admission,
            admissions=valid_admissions,
            blocker_events=blocker_events,
            generation_owners=generation_owners,
            owners=valid_owners,
            settlements=fully_valid_settlements,
            head_data=valid_heads.get(admission["threadId"]),
            thread_id=admission["threadId"],
            user_id=user_id,
        ):
            _mark_b1_validation_error(
                error_keys,
                _B1_ADMISSION_ERROR_KEYS,
                validation_log_state,
            )

    processed_collection_name = B1_SCAN_COLLECTIONS[-1]
    canonical_marker_ids = set()
    replay_attempt_states = {}
    legacy_marker_count = 0
    if readable[processed_collection_name]:
        processed_documents = scans[processed_collection_name]
        for document_id, data in processed_documents.items():
            if set(data) & _B1_PROCESSED_OWNERSHIP_FIELDS:
                canonical_marker_ids.add(document_id)
                if not _valid_processed_projection_shape(document_id, data):
                    _mark_b1_validation_error(
                        error_keys,
                        _B1_COLLECTION_ERROR_KEYS[processed_collection_name],
                        validation_log_state,
                    )
                    continue
                canonical_source_id = data["canonicalSourceId"]
                settlement_data = structurally_valid_settlements.get(
                    canonical_source_id
                )
                if settlement_data is None:
                    _mark_b1_validation_error(
                        error_keys,
                        {"b1MarkerOrSettlementAmbiguities"},
                        validation_log_state,
                    )
                    continue
                descriptor = next(
                    (
                        item
                        for item in descriptors_by_source.get(
                            canonical_source_id,
                            (),
                        )
                        if item["sourceAliasKey"] == document_id
                    ),
                    None,
                )
                if descriptor is None:
                    ambiguity_tokens.add(("processed", document_id))
                    continue
                try:
                    _validate_processed_alias_projection(
                        data,
                        descriptor=descriptor,
                        canonical_source_id=canonical_source_id,
                        settlement_data=settlement_data,
                    )
                except SourceSettlementConflict:
                    ambiguity_tokens.add(("processed", document_id))
                except Exception:
                    _mark_b1_validation_error(
                        error_keys,
                        {"b1MarkerOrSettlementAmbiguities"},
                        validation_log_state,
                    )
                continue

            if (
                set(data) == {"processedAt"}
                and _is_aware_datetime(data.get("processedAt"))
            ):
                legacy_marker_count += 1
                continue
            if (
                set(data) == {"status", "replayAttemptId", "claimedAt"}
                and data.get("status") == "operator_replay_in_progress"
                and type(data.get("replayAttemptId")) is str
                and bool(data.get("replayAttemptId"))
                and _is_aware_datetime(data.get("claimedAt"))
            ):
                replay_attempt_states.setdefault(
                    data["replayAttemptId"],
                    set(),
                ).add("in_progress")
                continue
            if (
                set(data)
                == {
                    "status",
                    "replayAttemptId",
                    "claimedAt",
                    "processedAt",
                }
                and data.get("status") == "processed"
                and type(data.get("replayAttemptId")) is str
                and bool(data.get("replayAttemptId"))
                and _is_aware_datetime(data.get("claimedAt"))
                and _is_aware_datetime(data.get("processedAt"))
            ):
                replay_attempt_states.setdefault(
                    data["replayAttemptId"],
                    set(),
                ).add("completed")
                continue
            _mark_b1_validation_error(
                error_keys,
                _B1_COLLECTION_ERROR_KEYS[processed_collection_name],
                validation_log_state,
            )

        for canonical_source_id in fully_valid_settlements:
            for descriptor in descriptors_by_source[canonical_source_id]:
                if descriptor["sourceAliasKey"] not in canonical_marker_ids:
                    ambiguity_tokens.add(
                        (
                            "processed-missing",
                            canonical_source_id,
                            descriptor["sourceAliasKey"],
                        )
                    )

    completed_replay_attempt_count = sum(
        "in_progress" not in states
        for states in replay_attempt_states.values()
    )
    quarantined_replay_attempt_count = sum(
        "in_progress" in states
        for states in replay_attempt_states.values()
    )
    counts["b1MarkerOrSettlementAmbiguities"] = len(ambiguity_tokens)
    counts["b1LegacyMarkerOnlyAmbiguous"] = (
        legacy_marker_count + completed_replay_attempt_count
    )
    counts["b1LegacyReplayClaimQuarantined"] = (
        quarantined_replay_attempt_count
    )
    for key in error_keys:
        counts[key] = COUNT_ERROR
    return counts


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
    queues.update(_collect_b1_health_counts(user_ref, user_id=user_id))

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
