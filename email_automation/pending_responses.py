"""
Pending Responses Queue

Handles retry logic for failed AI-generated response emails.
Similar to outbox retry, but for responses that fail to send after processing.
"""

from datetime import datetime, timedelta, timezone
import hashlib
from typing import Optional, Dict, Any, List
from uuid import uuid4
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter

from .sent_mail_guard import (
    SentMailGuardLookupError,
    find_exact_sent_message_by_immutable_id,
    find_sent_conversation_continuation_for_retry,
    find_matching_sent_message_for_retry,
    sent_after_from_retry_data,
)
from .outbound_safety import validate_outbound_body
from .campaign_safety import (
    CAMPAIGN_AUTOMATION_ALLOW,
    CAMPAIGN_AUTOMATION_BLOCKED,
    get_client_automation_decision,
)
from .column_config import (
    get_column_config_error,
    response_requests_nonrequestable_fields,
)
from .send_permits import (
    GraphSendPermitBlocked,
    PENDING_COMPLETION_OBLIGATION_COLLECTION,
    PENDING_GRAPH_SENT_RECHECK_LIMIT,
    _stable_evidence_hash,
    _validate_permit,
    assert_pending_claim_allowed,
    cas_pending_claim_transition,
    has_terminal_send_marker,
    issue_pending_graph_send_permit,
    operator_resolve_pending_graph_draft_review,
    operator_settle_pending_graph_send_review,
    pending_envelope_hash,
    pending_completion_obligation_payload,
    read_expired_pending_graph_send_permit,
    read_permit,
    read_pending_graph_send_operator_settlement_replay,
    reconcile_pending_graph_send_permit,
    resolve_graph_send_permit,
    validate_pending_completion_obligation_payload,
)

# Maximum retry attempts before giving up
MAX_RESPONSE_ATTEMPTS = 5
PENDING_RESPONSE_SEND_LEASE_SECONDS = 300
PENDING_COMPLETION_SCAN_LIMIT = 500
_CLIENT_COMPLETION_INELIGIBLE_STATUSES = frozenset({
    "stopping",
    "stopped",
    "archived",
    "deleted",
})


def resolve_pending_graph_draft_review(
    user_id: str,
    thread_id: str,
    *,
    expected_permit_id: str,
    expected_permit_hash: str,
    expected_review_evidence_hash: str,
    operator_id: str,
    operator_reason: str,
    settlement_id: str,
) -> str:
    """Resolve exact retained draft evidence without touching the provider."""
    from .clients import _fs

    user_ref = _fs.collection("users").document(user_id)
    thread_ref = user_ref.collection("threads").document(thread_id)
    review_ref = user_ref.collection("graphSendDraftReviews").document(
        f"pending-{expected_permit_id}"
    )
    audit_ref = user_ref.collection(
        "graphSendDraftReviewSettlements"
    ).document(settlement_id)
    operator_resolve_pending_graph_draft_review(
        _fs,
        thread_ref,
        expected_permit_id=expected_permit_id,
        expected_permit_hash=expected_permit_hash,
        expected_review_evidence_hash=expected_review_evidence_hash,
        review_ref=review_ref,
        action="confirm_retained_draft_not_actionable",
        operator_id=operator_id,
        operator_reason=operator_reason,
        settlement_id=settlement_id,
        audit_ref=audit_ref,
    )
    return "settled_draft_review_resolved"


def acknowledge_pending_graph_send_ambiguity(
    user_id: str,
    pending_document_id: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    headers_factory=None,
    expected_permit_id: str,
    expected_permit_hash: str,
    expected_reconciliation_evidence_hash: str,
    operator_id: str,
    operator_reason: str,
    settlement_id: str,
) -> str:
    """Freshly recheck Sent, then exact-CAS sent or unknown-no-retry."""
    from .clients import _fs

    user_ref = _fs.collection("users").document(user_id)
    audit_ref = user_ref.collection("graphSendOperatorSettlements").document(
        settlement_id
    )
    replay_status = read_pending_graph_send_operator_settlement_replay(
        _fs,
        user_ref,
        audit_ref,
        pending_document_id=pending_document_id,
        expected_permit_id=expected_permit_id,
        expected_permit_hash=expected_permit_hash,
        expected_reconciliation_evidence_hash=(
            expected_reconciliation_evidence_hash
        ),
        operator_id=operator_id,
        operator_reason=operator_reason,
        settlement_id=settlement_id,
    )
    if replay_status is not None:
        return replay_status

    pending_ref = user_ref.collection("pendingResponses").document(
        pending_document_id
    )
    pending_snapshot = pending_ref.get()
    if not pending_snapshot.exists:
        raise GraphSendPermitBlocked(
            "operator settlement pending response is missing"
        )
    loaded_data = pending_snapshot.to_dict() or {}
    thread_id = str(loaded_data.get("threadId") or "").strip()
    evidence_ref = loaded_data.get("graphSendReviewEvidenceRef")
    if not thread_id or evidence_ref is None:
        raise GraphSendPermitBlocked(
            "operator settlement pending review linkage is missing"
        )
    thread_ref = user_ref.collection("threads").document(thread_id)
    permit = read_expired_pending_graph_send_permit(
        _fs,
        thread_ref,
        pending_ref,
        loaded_data,
    )
    if (
        not permit
        or permit.get("permitId") != expected_permit_id
        or permit.get("immutableHash") != expected_permit_hash
        or permit.get("pendingReconciliationEvidenceHash")
        != expected_reconciliation_evidence_hash
    ):
        raise GraphSendPermitBlocked(
            "operator settlement retained permit is not exact"
        )
    request_started_at = permit.get("requestStartedAt")
    if request_started_at is None:
        raise GraphSendPermitBlocked(
            "operator settlement permit has no provider send boundary"
        )
    if headers is None:
        if headers_factory is None:
            raise GraphSendPermitBlocked(
                "operator settlement requires server-owned mailbox authorization"
            )
        headers = headers_factory()
    try:
        prepared_envelope = permit.get("preparedEnvelope") or {}
        sent_match = find_exact_sent_message_by_immutable_id(
            headers,
            prepared_envelope.get("draftId"),
            recipient=loaded_data.get("recipient"),
            to_recipients=prepared_envelope.get("toRecipients"),
            cc_recipients=prepared_envelope.get("ccRecipients"),
            require_no_bcc=True,
            require_attachment_proof=True,
            canonical_body_hash=prepared_envelope.get("htmlBodyHash"),
            subject=prepared_envelope.get("subject"),
            conversation_id=loaded_data.get("conversationId"),
            attempts=2,
        )
    except Exception as exc:
        raise GraphSendPermitBlocked(
            "operator settlement requires a fresh readable Sent Items check"
        ) from exc
    lookup_completed_at = datetime.now(timezone.utc)
    if sent_match:
        sent_evidence = {
            **dict(sent_match),
            "sentMessageId": sent_match.get("sentMessageId") or sent_match.get("id"),
            "recipient": permit.get("recipient"),
            "bodyHash": permit.get("bodyHash"),
            "conversationId": (
                sent_match.get("conversationId") or permit.get("conversationId")
            ),
            "permitId": expected_permit_id,
            "sourceGraphMessageId": permit.get("sourceGraphMessageId"),
            "preparedEnvelopeHash": permit.get("sendPreparedEnvelopeHash"),
        }
        review_snapshot = evidence_ref.get()
        review_data = review_snapshot.to_dict() if review_snapshot.exists else {}
        if not review_snapshot.exists:
            raise GraphSendPermitBlocked(
                "operator settlement server-owned review evidence is missing"
            )
        reconcile_pending_graph_send_permit(
            _fs,
            thread_ref,
            pending_ref,
            loaded_data,
            outcome="sent",
            sent_evidence=sent_evidence,
            evidence_document=(
                evidence_ref,
                {
                    **review_data,
                    "status": "reconciled_sent",
                    "alreadySent": True,
                    "sendOutcomeUnknown": False,
                    "retryAllowed": False,
                    "sentMessageId": sent_evidence.get("sentMessageId"),
                    "internetMessageId": sent_match.get("internetMessageId"),
                    "sentDateTime": sent_match.get("sentDateTime"),
                    "freshSentLookupCompletedAt": lookup_completed_at,
                    "resolvedBy": operator_id,
                },
            ),
            operator_audit_document=(
                audit_ref,
                {
                    "version": 1,
                    "settlementId": settlement_id,
                    "action": "acknowledge_ambiguous_no_retry",
                    "requestedAction": "acknowledge_ambiguous_no_retry",
                    "operatorId": operator_id,
                    "operatorReason": operator_reason,
                    "reconciliationEvidenceHash": (
                        expected_reconciliation_evidence_hash
                    ),
                    "resolution": "exact_sent",
                    "alreadySent": True,
                    "retryAllowed": False,
                    "freshSentLookupCompletedAt": lookup_completed_at,
                    "resolvedAt": lookup_completed_at,
                    "sentMessageId": sent_evidence.get("sentMessageId"),
                },
            ),
            completion_document=_pending_completion_side_document(
                user_id,
                user_ref,
                pending_ref,
                loaded_data,
                permit_id=str(permit.get("permitId") or ""),
                permit_immutable_hash=str(
                    permit.get("immutableHash") or ""
                ),
                sent_evidence=sent_evidence,
            ),
        )
        return "settled_sent"

    operator_settle_pending_graph_send_review(
        _fs,
        thread_ref,
        pending_ref,
        loaded_data,
        expected_permit_id=expected_permit_id,
        expected_permit_hash=expected_permit_hash,
        expected_reconciliation_evidence_hash=(
            expected_reconciliation_evidence_hash
        ),
        reconciliation_evidence_ref=evidence_ref,
        action="acknowledge_ambiguous_no_retry",
        operator_id=operator_id,
        operator_reason=operator_reason,
        settlement_id=settlement_id,
        audit_ref=audit_ref,
        sent_lookup_completed_at=lookup_completed_at,
    )
    return "settled_ambiguous_no_retry"


def _preserve_pending_campaign_suppression(doc, decision) -> None:
    doc.reference.update({
        "status": "queued",
        "processingBy": None,
        "processingAt": None,
        "processingLeaseUntil": None,
        "automationSuppressedState": decision.state,
        "automationSuppressedReason": decision.reason,
        "automationSuppressedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    })


def _same_pending_response_intent(
    current: Dict[str, Any],
    loaded: Dict[str, Any],
) -> bool:
    """Compare only the immutable envelope/body that authorizes a retry."""
    for field in (
        "threadId",
        "msgId",
        "clientId",
        "conversationId",
        "responseBody",
    ):
        if (current or {}).get(field) != (loaded or {}).get(field):
            return False
    return str((current or {}).get("recipient") or "").strip().lower() == str(
        (loaded or {}).get("recipient") or ""
    ).strip().lower()


def _has_terminal_pending_send_marker(thread_data: Dict[str, Any]) -> bool:
    """Fail closed on any durable marker that may own terminal reply work."""
    return has_terminal_send_marker(thread_data)


def _claim_pending_response_for_send(
    user_id: str,
    doc,
    loaded_data: Dict[str, Any],
) -> Optional[str]:
    """CAS the pending doc only when no terminal saga currently owns the thread."""
    from .clients import _fs

    thread_id = str((loaded_data or {}).get("threadId") or "").strip()
    if not thread_id or not getattr(doc, "id", None):
        return None
    user_ref = _fs.collection("users").document(user_id)
    thread_ref = user_ref.collection("threads").document(thread_id)
    pending_ref = user_ref.collection("pendingResponses").document(doc.id)
    transaction = _fs.transaction()
    try:
        thread_snapshot = thread_ref.get(transaction=transaction)
        pending_snapshot = pending_ref.get(transaction=transaction)
        if not thread_snapshot.exists or not pending_snapshot.exists:
            return None
        thread_data = thread_snapshot.to_dict() or {}
        current_data = pending_snapshot.to_dict() or {}
        if _has_terminal_pending_send_marker(thread_data):
            return None
        try:
            assert_pending_claim_allowed(
                transaction,
                thread_ref,
                thread_data=thread_data,
            )
        except GraphSendPermitBlocked:
            # An expired queue lease does not make an in-flight/ambiguous send
            # safe to repeat.  Its retained permit must be reconciled and
            # issuer-settled first.
            return None
        if not _same_pending_response_intent(current_data, loaded_data):
            return None

        now = datetime.now(timezone.utc)
        current_owner = current_data.get("processingBy")
        current_lease = current_data.get("processingLeaseUntil")
        if isinstance(current_lease, datetime):
            if current_lease.tzinfo is None:
                current_lease = current_lease.replace(tzinfo=timezone.utc)
            else:
                current_lease = current_lease.astimezone(timezone.utc)
        else:
            current_lease = None
        if current_owner and (
            current_lease is None or current_lease > now
        ):
            return None

        claim_token = f"pending-response-{uuid4().hex}"
        transaction.update(pending_ref, {
            "status": "sending",
            "processingBy": claim_token,
            "processingAt": now,
            "processingLeaseUntil": now + timedelta(
                seconds=PENDING_RESPONSE_SEND_LEASE_SECONDS
            ),
            "updatedAt": SERVER_TIMESTAMP,
        })
        transaction.commit()
        return claim_token
    except Exception as exc:
        raise RuntimeError(f"pending response send claim failed: {exc}") from exc


def _final_pending_response_send_fence(
    user_id: str,
    doc,
    loaded_data: Dict[str, Any],
    claim_token: str,
) -> Any:
    """Renew an unchanged claim immediately before Graph or release it to a saga.

    A terminal saga that commits after the early queue claim wins this final
    transaction.  Only the exact loaded envelope owned by ``claim_token`` may
    be released; replacement work or a newer worker is never changed.
    """
    from .clients import _fs

    thread_id = str((loaded_data or {}).get("threadId") or "").strip()
    if not thread_id or not claim_token or not getattr(doc, "id", None):
        return False
    user_ref = _fs.collection("users").document(user_id)
    thread_ref = user_ref.collection("threads").document(thread_id)
    pending_ref = user_ref.collection("pendingResponses").document(doc.id)
    try:
        return issue_pending_graph_send_permit(
            _fs,
            thread_ref,
            pending_ref,
            loaded_data,
            claim_token,
        )
    except Exception as exc:
        raise RuntimeError(
            f"pending response final send fence failed: {exc}"
        ) from exc


def _pending_claim_refs(user_id: str, doc, data: Dict[str, Any], capability=None):
    from .clients import _fs

    user_ref = _fs.collection("users").document(user_id)
    thread_ref = user_ref.collection("threads").document(data.get("threadId"))
    pending_ref = (
        capability.issuer_ref
        if capability is not None
        else getattr(doc, "reference", doc)
    )
    return _fs, user_ref, thread_ref, pending_ref


def _pending_exit_document_ref(user_ref, doc, data, exit_kind: str):
    identity = hashlib.sha256(
        f"{getattr(doc, 'id', '')}:{exit_kind}:{pending_envelope_hash(data)}".encode(
            "utf-8"
        )
    ).hexdigest()
    return user_ref.collection("deadLetterQueue").document(
        f"pending-exit-{identity}"
    )


def _require_pending_exit_cas(result: bool, exit_kind: str) -> bool:
    """Treat a missing/replaced exact claim as ownership loss, never success."""
    if result is not True:
        raise GraphSendPermitBlocked(
            f"pending response ownership was lost before {exit_kind} CAS"
        )
    return True


def _cas_pending_update(
    user_id: str,
    doc,
    data: Dict[str, Any],
    claim_token: str,
    patch: Dict[str, Any],
    *,
    capability=None,
    permit_settlement=None,
) -> bool:
    _fs, user_ref, thread_ref, pending_ref = _pending_claim_refs(
        user_id,
        doc,
        data,
        capability,
    )
    return _require_pending_exit_cas(
        cas_pending_claim_transition(
            _fs,
            thread_ref,
            pending_ref,
            data,
            claim_token,
            pending_patch=patch,
            capability=capability,
            permit_settlement=permit_settlement,
        ),
        "update",
    )


def _cas_pending_dead_letter(
    user_id: str,
    doc,
    data: Dict[str, Any],
    claim_token: str,
    reason: str,
    *,
    capability=None,
    permit_settlement=None,
    sent_match: Optional[Dict[str, Any]] = None,
    already_sent: bool = False,
) -> bool:
    _fs, user_ref, thread_ref, pending_ref = _pending_claim_refs(
        user_id,
        doc,
        data,
        capability,
    )
    exit_kind = "sent_reconciliation" if already_sent else "manual_review"
    dead_ref = _pending_exit_document_ref(user_ref, doc, data, exit_kind)
    payload = {
        **data,
        "source": "pendingResponses",
        "originalDocId": doc.id,
        "failureReason": reason,
        "status": "needs_reconciliation" if already_sent else "manual_review",
        "alreadySent": bool(already_sent),
        "deadLetteredAt": SERVER_TIMESTAMP,
        "movedAt": SERVER_TIMESTAMP,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    if sent_match:
        payload.update({
            "sentMessageId": sent_match.get("sentMessageId") or sent_match.get("id"),
            "internetMessageId": sent_match.get("internetMessageId"),
            "conversationId": sent_match.get("conversationId"),
            "sentDateTime": sent_match.get("sentDateTime"),
        })
    return _require_pending_exit_cas(
        cas_pending_claim_transition(
            _fs,
            thread_ref,
            pending_ref,
            data,
            claim_token,
            delete_pending=True,
            side_documents=((dead_ref, payload),),
            capability=capability,
            permit_settlement=permit_settlement,
        ),
        "dead-letter",
    )


def _pending_completion_side_document(
    user_id: str,
    user_ref,
    doc,
    data: Dict[str, Any],
    *,
    permit_id: str,
    permit_immutable_hash: str,
    sent_evidence: Dict[str, Any],
):
    raw_client_id = (data or {}).get("clientId")
    client_id = "" if raw_client_id is None else str(raw_client_id)
    thread_id = str((data or {}).get("threadId") or "")
    source_message_id = str((data or {}).get("msgId") or "")
    pending_document_id = str(getattr(doc, "id", None) or "")
    if (
        not thread_id
        or thread_id != thread_id.strip()
        or not source_message_id
        or source_message_id != source_message_id.strip()
        or not pending_document_id
        or pending_document_id != pending_document_id.strip()
        or client_id != client_id.strip()
    ):
        raise GraphSendPermitBlocked(
            "pending settled_sent completion identity is missing or "
            "non-canonical"
        )
    obligation_id, obligation_payload = (
        pending_completion_obligation_payload(
            user_id=user_id,
            client_id=client_id,
            thread_id=thread_id,
            pending_document_id=pending_document_id,
            source_graph_message_id=source_message_id,
            pending_envelope_hash_value=pending_envelope_hash(data),
            permit_id=permit_id,
            permit_immutable_hash=permit_immutable_hash,
            sent_evidence=sent_evidence,
            complete_client_after_reply=bool(client_id),
        )
    )
    obligation_ref = user_ref.collection(
        PENDING_COMPLETION_OBLIGATION_COLLECTION
    ).document(obligation_id)
    return obligation_ref, obligation_payload


def _cas_pending_success(
    user_id: str,
    doc,
    data: Dict[str, Any],
    claim_token: str,
    capability,
    sent_evidence: Dict[str, Any],
) -> bool:
    _fs, user_ref, thread_ref, pending_ref = _pending_claim_refs(
        user_id,
        doc,
        data,
        capability,
    )
    obligation_ref, obligation_payload = _pending_completion_side_document(
        user_id,
        user_ref,
        doc,
        data,
        permit_id=capability.permit_id,
        permit_immutable_hash=capability.immutable_hash,
        sent_evidence=sent_evidence,
    )
    return _require_pending_exit_cas(
        cas_pending_claim_transition(
            _fs,
            thread_ref,
            pending_ref,
            data,
            claim_token,
            delete_pending=True,
            capability=capability,
            permit_settlement="settled_sent",
            sent_evidence=sent_evidence,
            side_documents=((obligation_ref, obligation_payload),),
        ),
        "accepted-send success",
    )


def _cas_pending_ambiguity(
    user_id: str,
    doc,
    data: Dict[str, Any],
    claim_token: str,
    capability,
    reason: str,
) -> bool:
    _fs, _user_ref, thread_ref, pending_ref = _pending_claim_refs(
        user_id,
        doc,
        data,
        capability,
    )
    review_ref = thread_ref.collection("graphSendReviews").document(
        f"pending-{capability.permit_id}"
    )
    review_payload = {
        "threadId": data.get("threadId"),
        "clientId": data.get("clientId"),
        "pendingDocumentId": getattr(doc, "id", None),
        "status": "needs_reconciliation",
        "source": "pendingGraphSendProtocol",
        "authoritative": True,
        "alreadySent": None,
        "providerSendStarted": True,
        "sendOutcomeUnknown": True,
        "retryAllowed": False,
        "failureReason": reason,
        "graphSendPermitId": capability.permit_id,
        "graphSendPermitHash": capability.immutable_hash,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    return _require_pending_exit_cas(
        cas_pending_claim_transition(
            _fs,
            thread_ref,
            pending_ref,
            data,
            claim_token,
            pending_patch={
                "status": "needs_reconciliation",
                "lastError": reason,
                "updatedAt": SERVER_TIMESTAMP,
            },
            side_documents=((review_ref, review_payload),),
            capability=capability,
            permit_settlement="reconciliation_recorded",
        ),
        "ambiguous-send retention",
    )


def _cas_pending_draft_review(
    user_id: str,
    doc,
    data: Dict[str, Any],
    claim_token: str,
    capability,
    reason: str,
) -> bool:
    """Atomically retire one exact pre-send draft into authoritative review."""
    _fs, user_ref, thread_ref, pending_ref = _pending_claim_refs(
        user_id,
        doc,
        data,
        capability,
    )
    permit = read_permit(capability)
    preparation = dict(permit.get("draftPreparation") or {})
    resolution_evidence = dict(permit.get("resolutionEvidence") or {})
    prepared_envelope = dict(permit.get("preparedEnvelope") or {})
    review_ref = user_ref.collection("graphSendDraftReviews").document(
        f"pending-{capability.permit_id}"
    )
    review_payload = {
        "threadId": data.get("threadId"),
        "clientId": data.get("clientId"),
        "pendingDocumentId": getattr(doc, "id", None),
        "status": "manual_review",
        "source": "pendingGraphSendProtocol",
        "authoritative": True,
        "alreadySent": False,
        "providerSendStarted": False,
        "sendOutcomeUnknown": False,
        "retryAllowed": False,
        "automaticDeleteAttempted": resolution_evidence.get(
            "automaticDeleteAttempted",
            False,
        ),
        "failureReason": str(reason or "").strip(),
        "graphSendPermitId": capability.permit_id,
        "graphSendPermitHash": capability.immutable_hash,
        "sourceGraphMessageId": permit.get("sourceGraphMessageId"),
        "preparedEnvelopeHash": prepared_envelope.get(
            "preparedEnvelopeHash"
        ),
        "draftId": preparation.get("draftId"),
        "draftMutationState": preparation.get("state"),
        "draftResolutionEvidenceHash": permit.get(
            "resolutionEvidenceHash"
        ),
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }

    def persist_exact_review() -> bool:
        return _require_pending_exit_cas(
            cas_pending_claim_transition(
                _fs,
                thread_ref,
                pending_ref,
                data,
                claim_token,
                delete_pending=True,
                side_documents=((review_ref, review_payload),),
                capability=capability,
                permit_settlement="settled_draft_needs_review",
            ),
            "draft-review",
        )

    try:
        return persist_exact_review()
    except Exception as first_error:
        try:
            return persist_exact_review()
        except Exception as readback_error:
            raise readback_error from first_error


def _reconcile_expired_pending_permit(
    user_id: str,
    headers: Dict[str, str],
    doc,
    data: Dict[str, Any],
) -> bool:
    """Transfer a lost-capability permit to exact durable evidence, never send."""
    _fs, user_ref, thread_ref, pending_ref = _pending_claim_refs(
        user_id,
        doc,
        data,
    )
    try:
        permit = read_expired_pending_graph_send_permit(
            _fs,
            thread_ref,
            pending_ref,
            data,
        )
    except GraphSendPermitBlocked as exc:
        if "active" in str(exc).lower():
            return False
        raise
    if not permit:
        return False

    permit_id = permit.get("permitId")
    preparation = dict(permit.get("draftPreparation") or {})
    request_started_at = permit.get("requestStartedAt")
    evidence_ref = _pending_exit_document_ref(
        user_ref,
        doc,
        data,
        f"graph_permit_{permit_id}",
    )
    base_evidence = {
        **data,
        "source": "pendingResponses",
        "originalDocId": doc.id,
        "graphSendPermitId": permit_id,
        "graphSendPermitHash": permit.get("immutableHash"),
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
        "movedAt": SERVER_TIMESTAMP,
        "deadLetteredAt": SERVER_TIMESTAMP,
    }

    if (
        permit.get("status") == "definitely_not_sent"
        and request_started_at is None
        and (
            not preparation
            or preparation.get("state")
            == "create_definitely_not_created"
        )
    ):
        resolution_evidence = dict(
            permit.get("resolutionEvidence") or {}
        )
        reconcile_pending_graph_send_permit(
            _fs,
            thread_ref,
            pending_ref,
            data,
            outcome="definitely_not_sent",
            evidence_document=(
                evidence_ref,
                {
                    **base_evidence,
                    "status": "retryable",
                    "alreadySent": False,
                    "providerSendStarted": False,
                    "failureReason": (
                        resolution_evidence.get("reason")
                        or "The retained Graph permit proves provider work did not start"
                    ),
                },
            ),
        )
        return True

    if permit.get("status") == "issued" and not preparation:
        reconcile_pending_graph_send_permit(
            _fs,
            thread_ref,
            pending_ref,
            data,
            outcome="definitely_not_started",
            evidence_document=(
                evidence_ref,
                {
                    **base_evidence,
                    "status": "retryable",
                    "alreadySent": False,
                    "providerSendStarted": False,
                    "failureReason": (
                        "Expired Graph permit had no provider mutation; safely "
                        "released for a fresh retry"
                    ),
                },
            ),
        )
        return True

    if request_started_at is None:
        resolution_evidence = dict(permit.get("resolutionEvidence") or {})
        evidence_ref = user_ref.collection("graphSendDraftReviews").document(
            f"pending-{permit_id}"
        )
        reconcile_pending_graph_send_permit(
            _fs,
            thread_ref,
            pending_ref,
            data,
            outcome="draft_needs_review",
            evidence_document=(
                evidence_ref,
                {
                    **base_evidence,
                    "source": "pendingGraphSendProtocol",
                    "status": "manual_review",
                    "authoritative": True,
                    "alreadySent": False,
                    "providerSendStarted": False,
                    "sendOutcomeUnknown": False,
                    "retryAllowed": False,
                    "automaticDeleteAttempted": False,
                    "draftId": preparation.get("draftId"),
                    "draftMutationState": preparation.get("state"),
                    "draftResolutionEvidenceHash": permit.get(
                        "resolutionEvidenceHash"
                    ),
                    "failureReason": resolution_evidence.get("reason"),
                },
            ),
        )
        return True

    prior_rechecks = data.get("graphSendSentRecheckCount", 0)
    if type(prior_rechecks) is not int or prior_rechecks < 0:
        raise GraphSendPermitBlocked(
            "pending send reconciliation recheck count is malformed"
        )
    if prior_rechecks >= PENDING_GRAPH_SENT_RECHECK_LIMIT:
        # This remains operator-visible and blocks every new send.  A capped
        # scanner must not turn a long-lived ambiguous provider result into an
        # unbounded Graph read loop.
        return True

    sent_match = None
    lookup_error = None
    try:
        prepared_envelope = permit.get("preparedEnvelope") or {}
        sent_match = find_exact_sent_message_by_immutable_id(
            headers,
            prepared_envelope.get("draftId"),
            recipient=data.get("recipient"),
            to_recipients=prepared_envelope.get("toRecipients"),
            cc_recipients=prepared_envelope.get("ccRecipients"),
            require_no_bcc=True,
            require_attachment_proof=True,
            canonical_body_hash=prepared_envelope.get("htmlBodyHash"),
            subject=prepared_envelope.get("subject"),
            conversation_id=data.get("conversationId"),
            attempts=2,
        )
    except SentMailGuardLookupError as exc:
        lookup_error = str(exc)

    if sent_match:
        sent_evidence = {
            **dict(sent_match),
            "sentMessageId": (
                sent_match.get("sentMessageId") or sent_match.get("id")
            ),
            "recipient": permit.get("recipient"),
            "bodyHash": permit.get("bodyHash"),
            "conversationId": (
                sent_match.get("conversationId")
                or permit.get("conversationId")
            ),
            "permitId": permit_id,
            "sourceGraphMessageId": permit.get("sourceGraphMessageId"),
            "preparedEnvelopeHash": permit.get("sendPreparedEnvelopeHash"),
        }
        reconcile_pending_graph_send_permit(
            _fs,
            thread_ref,
            pending_ref,
            data,
            outcome="sent",
            sent_evidence=sent_evidence,
            evidence_document=(
                evidence_ref,
                {
                    **base_evidence,
                    "status": "reconciled_sent",
                    "alreadySent": True,
                    "providerSendStarted": True,
                    "failureReason": (
                        "Expired Graph permit matched exact Sent Items evidence; "
                        "duplicate retry suppressed"
                    ),
                    "sentMessageId": sent_evidence.get("sentMessageId"),
                    "internetMessageId": sent_match.get("internetMessageId"),
                    "sentDateTime": sent_match.get("sentDateTime"),
                },
            ),
            completion_document=_pending_completion_side_document(
                user_id,
                user_ref,
                pending_ref,
                data,
                permit_id=str(permit.get("permitId") or ""),
                permit_immutable_hash=str(
                    permit.get("immutableHash") or ""
                ),
                sent_evidence=sent_evidence,
            ),
        )
        return True

    # Ambiguous /send evidence is server-owned protocol state, not a generic
    # dead-letter.  The dashboard's generic review actions must never be able
    # to hide it or turn `alreadySent=False` into a new outbox send.
    evidence_ref = thread_ref.collection("graphSendReviews").document(
        f"pending-{permit_id}"
    )
    reconcile_pending_graph_send_permit(
        _fs,
        thread_ref,
        pending_ref,
        data,
        outcome="send_needs_review",
        evidence_document=(
            evidence_ref,
            {
                **base_evidence,
                "status": "needs_reconciliation",
                # Tri-state on purpose: the provider request crossed /send and
                # neither Sent Items nor a response proved acceptance.  False
                # would be misread by generic review tooling as safe to resend.
                "alreadySent": None,
                "providerSendStarted": True,
                "sendOutcomeUnknown": True,
                "failureReason": (
                    "Expired Graph send permit has no exact Sent Items match; "
                    "manual reconciliation required"
                    + (f": {lookup_error}" if lookup_error else "")
                ),
            },
        ),
    )
    return True


def _pending_response_column_contract_error(data: Dict[str, Any], decision) -> Optional[str]:
    client_data = getattr(decision, "client_data", None) or {}
    column_config = client_data.get("columnConfig")
    config_error = get_column_config_error(column_config)
    if config_error:
        return f"Pending response has invalid persisted columnConfig: {config_error}"
    if response_requests_nonrequestable_fields(data.get("responseBody"), column_config):
        return "Pending response requests a non-requestable Note, Skip, or formula field"
    return None


def _gate_pending_response(
    user_id: str,
    doc,
    data: Dict[str, Any],
    decision=None,
) -> bool:
    decision = decision or get_client_automation_decision(user_id, data.get("clientId"))
    if decision.state == CAMPAIGN_AUTOMATION_ALLOW:
        contract_error = _pending_response_column_contract_error(data, decision)
        if contract_error:
            _move_pending_response_to_dead_letter(
                user_id,
                doc,
                data,
                f"{contract_error}; manual review required before retry",
            )
            return True
        return False
    if decision.state == CAMPAIGN_AUTOMATION_BLOCKED and decision.metadata.get("terminal"):
        _move_pending_response_to_dead_letter(
            user_id,
            doc,
            data,
            f"Client campaign is stopped; pending reply canceled: {decision.reason}",
        )
        return True
    _preserve_pending_campaign_suppression(doc, decision)
    return True


def _move_pending_response_to_dead_letter(user_id: str, doc, data: Dict[str, Any], reason: str) -> None:
    from .clients import _fs

    dead_letter_ref = _fs.collection("users").document(user_id).collection("deadLetterQueue")
    dead_letter_ref.add({
        **data,
        "source": "pendingResponses",
        "originalDocId": doc.id,
        "failureReason": reason,
        "deadLetteredAt": SERVER_TIMESTAMP,
        "movedAt": SERVER_TIMESTAMP,
    })
    doc.reference.delete()


def record_sent_unindexed_response(
    user_id: str,
    thread_id: str,
    msg_id: str,
    recipient: str,
    response_body: str,
    client_id: Optional[str] = None,
    reason: Optional[str] = None,
    *,
    source_context: str = "autoResponse",
    original_doc_id: Optional[str] = None,
    sent_match: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a reply that Graph accepted but the worker could not index.

    The email may already be in the sender mailbox, so this must be visible to
    operators without re-queuing the same body for another send attempt.
    """
    from .clients import _fs

    payload = {
        "threadId": thread_id,
        "msgId": msg_id,
        "recipient": recipient,
        "responseBody": response_body,
        "clientId": client_id,
        "source": source_context,
        "status": "needs_reconciliation",
        "alreadySent": True,
        "failureReason": reason or "Graph accepted reply but sent-message indexing failed",
        "deadLetteredAt": SERVER_TIMESTAMP,
        "movedAt": SERVER_TIMESTAMP,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    if original_doc_id:
        payload["originalDocId"] = original_doc_id
    if sent_match:
        payload.update({
            "sentMessageId": sent_match.get("sentMessageId") or sent_match.get("id"),
            "internetMessageId": sent_match.get("internetMessageId"),
            "conversationId": sent_match.get("conversationId"),
            "sentDateTime": sent_match.get("sentDateTime"),
        })

    _fs.collection("users").document(user_id).collection("deadLetterQueue").add(payload)


def queue_pending_response(
    user_id: str,
    thread_id: str,
    msg_id: str,
    recipient: str,
    response_body: str,
    client_id: Optional[str] = None,
    error: Optional[str] = None,
    *,
    subject: Optional[str] = None,
    conversation_id: Optional[str] = None,
    last_send_attempt_at: Optional[Any] = None,
) -> str:
    """
    Queue a failed response for later retry.

    Returns the document ID of the queued response.
    """
    from .clients import _fs

    pending_ref = _fs.collection("users").document(user_id).collection("pendingResponses")

    doc_data = {
        "threadId": thread_id,
        "msgId": msg_id,
        "recipient": recipient,
        "responseBody": response_body,
        "clientId": client_id,
        "attempts": 1,
        "lastError": error,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    if subject:
        doc_data["subject"] = subject
    if conversation_id:
        doc_data["conversationId"] = conversation_id
    if last_send_attempt_at:
        doc_data["lastSendAttemptAt"] = last_send_attempt_at

    # Use thread_id as doc ID to prevent duplicates
    doc_ref = pending_ref.document(thread_id)

    # Check if already exists
    existing = doc_ref.get()
    if existing.exists:
        # Update existing entry
        existing_data = existing.to_dict()
        doc_data["attempts"] = existing_data.get("attempts", 0) + 1
        doc_data["createdAt"] = existing_data.get("createdAt")  # Preserve original
        doc_ref.set(doc_data)
        print(f"📝 Updated pending response for thread {thread_id[:30]}... (attempt {doc_data['attempts']})")
    else:
        doc_ref.set(doc_data)
        print(f"📝 Queued pending response for thread {thread_id[:30]}...")

    return doc_ref.id


def get_pending_responses(user_id: str, *, apply_send_gates: bool = True) -> list:
    """
    Get all pending responses that haven't exceeded max attempts.
    """
    from .clients import _fs

    pending_ref = _fs.collection("users").document(user_id).collection("pendingResponses")
    docs = list(pending_ref.stream())

    valid = []
    for doc in docs:
        data = doc.to_dict()
        attempts = data.get("attempts", 0)

        if apply_send_gates and _gate_pending_response(user_id, doc, data):
            continue

        if not apply_send_gates:
            valid.append({
                "doc": doc,
                "data": data,
            })
            continue

        if attempts >= MAX_RESPONSE_ATTEMPTS:
            reason = data.get("lastError") or f"Exceeded max attempts ({MAX_RESPONSE_ATTEMPTS})"
            print(f"☠️ Pending response exceeded max attempts ({MAX_RESPONSE_ATTEMPTS}): {doc.id[:30]}...")
            _move_pending_response_to_dead_letter(user_id, doc, data, reason)
            continue

        valid.append({
            "doc": doc,
            "data": data,
        })

    return valid


def _pending_response_operation_state(
    status: str,
    recipient: Optional[str] = None,
    error: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build a Graph operation-state for a pending-response send outcome.

    Shape matches ``main._combine_graph_operation_states`` (GO-condition #3).
    """
    state: Dict[str, Any] = {"status": status, "operation": "pending_response_send"}
    if recipient:
        state["recipient"] = recipient
    if error is not None:
        state["error"] = str(error)[:1500]
    return state


def _pending_completion_operation_state(
    status: str,
    *,
    obligation_id: Optional[str] = None,
    error: Optional[Any] = None,
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "status": status,
        "operation": "pending_response_completion",
    }
    if obligation_id:
        state["obligationId"] = obligation_id
    if error is not None:
        state["error"] = str(error)[:1500]
    return state


def _canonical_ref_path(document_ref) -> Optional[str]:
    path = getattr(document_ref, "path", None)
    if not isinstance(path, str):
        return None
    normalized = path.strip("/")
    return normalized or None


def _pending_completion_linkage(
    user_id: str,
    user_ref,
    obligation_snapshot,
    *,
    transaction=None,
):
    obligation_ref = getattr(obligation_snapshot, "reference", None)
    if obligation_ref is None:
        obligation_ref = obligation_snapshot
    obligation_id = str(
        getattr(obligation_snapshot, "id", None)
        or getattr(obligation_ref, "id", None)
        or ""
    )
    raw = (
        obligation_snapshot.to_dict()
        if hasattr(obligation_snapshot, "to_dict")
        else {}
    ) or {}
    obligation = validate_pending_completion_obligation_payload(
        raw,
        document_id=obligation_id,
        expected_user_id=user_id,
    )
    immutable = obligation["immutable"]
    user_path = _canonical_ref_path(user_ref)
    obligation_path = _canonical_ref_path(obligation_ref)
    if user_path is not None and obligation_path != (
        f"{user_path}/{PENDING_COMPLETION_OBLIGATION_COLLECTION}/"
        f"{obligation_id}"
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation is outside its canonical user root"
        )

    thread_ref = user_ref.collection("threads").document(
        immutable["threadId"]
    )
    permit_ref = thread_ref.collection("graphSendPermits").document(
        immutable["permitId"]
    )
    pending_ref = user_ref.collection("pendingResponses").document(
        immutable["pendingDocumentId"]
    )
    client_ref = None
    if immutable["completeClientAfterReply"]:
        client_ref = user_ref.collection("clients").document(
            immutable["clientId"]
        )
    thread_snapshot = thread_ref.get(transaction=transaction)
    permit_snapshot = permit_ref.get(transaction=transaction)
    pending_snapshot = pending_ref.get(transaction=transaction)
    client_snapshot = (
        client_ref.get(transaction=transaction)
        if client_ref is not None
        else None
    )
    if (
        getattr(thread_snapshot, "exists", False) is not True
        or getattr(permit_snapshot, "exists", False) is not True
        or getattr(pending_snapshot, "exists", False) is True
        or (
            client_snapshot is not None
            and getattr(client_snapshot, "exists", False) is not True
        )
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation exact local evidence is missing"
        )
    permit = _validate_permit(permit_snapshot.to_dict() or {})
    thread_data = thread_snapshot.to_dict() or {}
    expected_pending_path = (
        f"{user_path}/pendingResponses/{immutable['pendingDocumentId']}"
        if user_path is not None
        else None
    )
    permit_issuer_path = str(permit.get("issuerDocumentPath") or "")
    if (
        permit.get("status") != "settled_sent"
        or permit.get("issuerKind") != "pending_response"
        or permit.get("issuerDocumentId")
        != immutable["pendingDocumentId"]
        or (
            expected_pending_path is not None
            and permit_issuer_path != expected_pending_path
        )
        or permit.get("permitId") != immutable["permitId"]
        or permit.get("immutableHash")
        != immutable["permitImmutableHash"]
        or permit.get("threadId") != immutable["threadId"]
        or str(permit.get("clientId") or "") != immutable["clientId"]
        or permit.get("sourceGraphMessageId")
        != immutable["sourceGraphMessageId"]
        or permit.get("envelopeHash")
        != immutable["pendingEnvelopeHash"]
        or _stable_evidence_hash(
            permit.get("terminalSentEvidence") or {}
        )
        != immutable["sentEvidenceHash"]
        or (
            immutable["completeClientAfterReply"]
            and (
                type(thread_data.get("clientId")) is not str
                or thread_data.get("clientId") != immutable["clientId"]
            )
        )
    ):
        raise GraphSendPermitBlocked(
            "pending completion obligation drifted from retained sent permit"
        )
    client_data = (
        client_snapshot.to_dict() or {}
        if client_snapshot is not None
        else {}
    )
    return {
        "obligation": obligation,
        "immutable": immutable,
        "obligationRef": obligation_ref,
        "threadRef": thread_ref,
        "permitRef": permit_ref,
        "clientRef": client_ref,
        "clientStatus": str(client_data.get("status") or "").strip().lower(),
    }


def _settle_pending_completion_obligation(
    user_id: str,
    obligation_snapshot,
    *,
    outcome: str,
) -> None:
    from .clients import _fs

    if outcome not in {
        "client_completed",
        "client_ineligible",
        "not_required",
    }:
        raise GraphSendPermitBlocked(
            "pending completion settlement outcome is invalid"
        )
    user_ref = _fs.collection("users").document(user_id)
    transaction = _fs.transaction()
    obligation_ref = getattr(obligation_snapshot, "reference", None)
    if obligation_ref is None:
        obligation_ref = obligation_snapshot
    current_snapshot = obligation_ref.get(transaction=transaction)
    if getattr(current_snapshot, "exists", False) is not True:
        raise GraphSendPermitBlocked(
            "pending completion obligation disappeared before settlement"
        )
    linkage = _pending_completion_linkage(
        user_id,
        user_ref,
        current_snapshot,
        transaction=transaction,
    )
    current = linkage["obligation"]
    completion_required = linkage["immutable"][
        "completeClientAfterReply"
    ]
    if (
        completion_required
        and outcome not in {"client_completed", "client_ineligible"}
    ) or (completion_required is False and outcome != "not_required"):
        raise GraphSendPermitBlocked(
            "pending completion settlement outcome conflicts with binding"
        )
    if current.get("status") == "settled":
        if current.get("completionOutcome") != outcome:
            raise GraphSendPermitBlocked(
                "pending completion settlement outcome drifted"
            )
        return
    now = datetime.now(timezone.utc)
    transaction.update(obligation_ref, {
        "status": "settled",
        "completionOutcome": outcome,
        "settledAt": now,
        "updatedAt": now,
    })
    transaction.commit()


def _replay_pending_completion_obligation(
    user_id: str,
    obligation_snapshot,
    processing_module,
) -> Dict[str, Any]:
    obligation_id = str(getattr(obligation_snapshot, "id", None) or "")
    try:
        from .clients import _fs

        user_ref = _fs.collection("users").document(user_id)
        linkage = _pending_completion_linkage(
            user_id,
            user_ref,
            obligation_snapshot,
        )
        obligation = linkage["obligation"]
        immutable = linkage["immutable"]
        if obligation.get("status") == "settled":
            return _pending_completion_operation_state(
                "healthy",
                obligation_id=obligation_id,
            )
        if immutable["completeClientAfterReply"] is False:
            _settle_pending_completion_obligation(
                user_id,
                obligation_snapshot,
                outcome="not_required",
            )
            return _pending_completion_operation_state(
                "healthy",
                obligation_id=obligation_id,
            )

        status = linkage["clientStatus"]
        if status == "completed":
            _settle_pending_completion_obligation(
                user_id,
                obligation_snapshot,
                outcome="client_completed",
            )
            return _pending_completion_operation_state(
                "healthy",
                obligation_id=obligation_id,
            )
        if status in _CLIENT_COMPLETION_INELIGIBLE_STATUSES:
            _settle_pending_completion_obligation(
                user_id,
                obligation_snapshot,
                outcome="client_ineligible",
            )
            return _pending_completion_operation_state(
                "healthy",
                obligation_id=obligation_id,
            )

        try:
            completed = processing_module._maybe_mark_client_completed(
                user_id,
                immutable["clientId"],
            )
        except Exception:
            completed = False
        if completed is True:
            _settle_pending_completion_obligation(
                user_id,
                obligation_snapshot,
                outcome="client_completed",
            )
            return _pending_completion_operation_state(
                "healthy",
                obligation_id=obligation_id,
            )

        client_snapshot = linkage["clientRef"].get()
        if getattr(client_snapshot, "exists", False) is not True:
            raise GraphSendPermitBlocked(
                "pending completion client disappeared after local replay"
            )
        status = str(
            (client_snapshot.to_dict() or {}).get("status") or ""
        ).strip().lower()
        if status == "completed":
            _settle_pending_completion_obligation(
                user_id,
                obligation_snapshot,
                outcome="client_completed",
            )
            return _pending_completion_operation_state(
                "healthy",
                obligation_id=obligation_id,
            )
        if status in _CLIENT_COMPLETION_INELIGIBLE_STATUSES:
            _settle_pending_completion_obligation(
                user_id,
                obligation_snapshot,
                outcome="client_ineligible",
            )
            return _pending_completion_operation_state(
                "healthy",
                obligation_id=obligation_id,
            )
        raise RuntimeError(
            "pending response client completion obligation remains unresolved"
        )
    except Exception as exc:
        return _pending_completion_operation_state(
            "error",
            obligation_id=obligation_id,
            error=exc,
        )


def _process_pending_completion_obligations(
    user_id: str,
    processing_module,
) -> List[Dict[str, Any]]:
    from .clients import _fs

    user_ref = _fs.collection("users").document(user_id)
    try:
        collection_ref = user_ref.collection(
            PENDING_COMPLETION_OBLIGATION_COLLECTION
        )
        query = collection_ref.where(
            filter=FieldFilter("status", "==", "owed")
        ).limit(PENDING_COMPLETION_SCAN_LIMIT + 1)
        snapshots = list(query.stream())
    except Exception as exc:
        return [_pending_completion_operation_state("error", error=exc)]
    if len(snapshots) > PENDING_COMPLETION_SCAN_LIMIT:
        return [_pending_completion_operation_state(
            "error",
            error=(
                "pending completion obligation scan exceeded its bounded limit"
            ),
        )]
    states = []
    for snapshot in snapshots:
        states.append(_replay_pending_completion_obligation(
            user_id,
            snapshot,
            processing_module,
        ))
    return states


def _get_local_campaign_suppression(getter=None):
    """Return suppression produced by this pending-response execution only."""
    if getter is None:
        from .processing import _get_reply_campaign_suppression
        getter = _get_reply_campaign_suppression
    return getter()


def process_pending_responses(user_id: str, headers: Dict[str, str]) -> List[Dict[str, Any]]:
    """
    Retry sending all pending responses.

    Returns a list of Graph operation-states (GO-condition #3): one per pending
    response that reached a send outcome, so a swallowed per-item Graph send
    failure now escalates the health rail via
    ``main._combine_graph_operation_states``.
    """
    from . import processing as processing_module

    send_reply_in_thread = processing_module.send_reply_in_thread
    reset_reply_send_outcome = getattr(
        processing_module,
        "_reset_reply_send_outcome",
        lambda: None,
    )
    get_reply_send_outcome = getattr(
        processing_module,
        "_get_reply_send_outcome",
        lambda: None,
    )

    operation_states: List[Dict[str, Any]] = []

    operation_states.extend(_process_pending_completion_obligations(
        user_id,
        processing_module,
    ))

    pending = get_pending_responses(user_id, apply_send_gates=False)

    if not pending:
        return operation_states

    print(f"\n📬 Found {len(pending)} pending response(s) to retry")

    for item in pending:
        doc = item["doc"]
        data = item["data"]
        pending_send_claim = None
        graph_send_capability = None

        thread_id = data.get("threadId")
        msg_id = data.get("msgId")
        recipient = data.get("recipient")
        response_body = data.get("responseBody")
        attempts = data.get("attempts", 0)

        print(f"  → Retrying response to {recipient} (attempt {attempts + 1}/{MAX_RESPONSE_ATTEMPTS})")

        try:
            pending_send_claim = _claim_pending_response_for_send(
                user_id,
                doc,
                data,
            )
            if not pending_send_claim:
                if _reconcile_expired_pending_permit(
                    user_id,
                    headers,
                    doc,
                    data,
                ):
                    print(
                        "    🧾 Reconciled expired retained Graph permit without "
                        "issuing another send"
                    )
                    continue
                print(
                    "    ⏸️ Pending response lost its pre-send fence or is owned "
                    "by an active terminal saga"
                )
                continue

            campaign_decision = get_client_automation_decision(
                user_id,
                data.get("clientId"),
            )
            contract_error = (
                _pending_response_column_contract_error(data, campaign_decision)
                if campaign_decision.state == CAMPAIGN_AUTOMATION_ALLOW
                else None
            )
            if contract_error:
                _cas_pending_dead_letter(
                    user_id,
                    doc,
                    data,
                    pending_send_claim,
                    f"{contract_error}; manual review required before retry",
                )
                print("    ⏸️ Pending response suppressed by current campaign state")
                continue
            if (
                campaign_decision.state == CAMPAIGN_AUTOMATION_BLOCKED
                and campaign_decision.metadata.get("terminal")
            ):
                _cas_pending_dead_letter(
                    user_id,
                    doc,
                    data,
                    pending_send_claim,
                    "Client campaign is stopped; pending reply canceled: "
                    f"{campaign_decision.reason}",
                )
                print("    ⏸️ Pending response suppressed by current campaign state")
                continue
            if campaign_decision.state != CAMPAIGN_AUTOMATION_ALLOW:
                _cas_pending_update(
                    user_id,
                    doc,
                    data,
                    pending_send_claim,
                    {
                        "status": "queued",
                        "processingBy": None,
                        "processingAt": None,
                        "processingLeaseUntil": None,
                        "automationSuppressedState": campaign_decision.state,
                        "automationSuppressedReason": campaign_decision.reason,
                        "automationSuppressedAt": SERVER_TIMESTAMP,
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                )
                print("    ⏸️ Pending response suppressed by current campaign state")
                continue

            if attempts >= MAX_RESPONSE_ATTEMPTS:
                reason = data.get("lastError") or f"Exceeded max attempts ({MAX_RESPONSE_ATTEMPTS})"
                _cas_pending_dead_letter(
                    user_id,
                    doc,
                    data,
                    pending_send_claim,
                    reason,
                )
                print(f"    ☠️ Pending response exceeded max attempts ({MAX_RESPONSE_ATTEMPTS})")
                continue

            body_validation = validate_outbound_body(response_body)
            if not body_validation.is_safe:
                _cas_pending_dead_letter(
                    user_id,
                    doc,
                    data,
                    pending_send_claim,
                    f"{body_validation.reason}; manual review required before retry",
                )
                print("    🛑 Unsafe pending response body moved to manual review before retry")
                continue

            if attempts > 0 or data.get("lastError"):
                try:
                    sent_match = find_matching_sent_message_for_retry(
                        headers,
                        recipient=recipient,
                        body=response_body,
                        subject=data.get("subject"),
                        conversation_id=data.get("conversationId"),
                        sent_after=sent_after_from_retry_data(data),
                    )
                except SentMailGuardLookupError as exc:
                    _cas_pending_dead_letter(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        f"Sent Items retry guard could not verify prior send; manual review required before retry: {exc}",
                    )
                    print("    ⚠️ Sent Items retry guard failed closed; moved pending response to manual review")
                    continue
                if sent_match:
                    _cas_pending_dead_letter(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        "Prior failed attempt appears already sent in Sent Items; stopped before retry",
                        sent_match=sent_match,
                        already_sent=True,
                    )
                    print("    ⚠️ Prior send found in Sent Items; moved to reconciliation without retrying")
                    continue
                try:
                    manual_continuation = find_sent_conversation_continuation_for_retry(
                        headers,
                        conversation_id=data.get("conversationId"),
                        sent_after=sent_after_from_retry_data(data),
                    )
                except SentMailGuardLookupError as exc:
                    _cas_pending_dead_letter(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        f"Sent Items retry guard could not verify manual continuation before retry; manual review required: {exc}",
                    )
                    print("    ⚠️ Manual continuation guard failed closed; moved pending response to manual review")
                    continue
                if manual_continuation:
                    _cas_pending_dead_letter(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        "Pending response stopped because Sent Items shows the user manually continued this conversation; review before retrying the stale draft.",
                    )
                    print("    ⚠️ Manual continuation found in Sent Items; moved pending response to manual review")
                    continue

            graph_send_capability = _final_pending_response_send_fence(
                user_id,
                doc,
                data,
                pending_send_claim,
            )
            if not graph_send_capability:
                print(
                    "    ⏸️ Pending response lost its final Graph fence or a "
                    "terminal saga won the race"
                )
                continue

            reset_reply_send_outcome()
            sent = send_reply_in_thread(
                user_id=user_id,
                headers=headers,
                body=response_body,
                current_msg_id=msg_id,
                recipient=recipient,
                thread_id=thread_id,
                graph_send_capability=graph_send_capability,
            )

            if sent:
                send_outcome = get_reply_send_outcome()
                exact_sent_evidence = getattr(
                    send_outcome, "exact_sent_evidence", None
                )
                if not isinstance(exact_sent_evidence, dict):
                    raise GraphSendPermitBlocked(
                        "indexed pending send lacks exact immutable Sent evidence"
                    )
                if not _cas_pending_success(
                    user_id,
                    doc,
                    data,
                    pending_send_claim,
                    graph_send_capability,
                    exact_sent_evidence,
                ):
                    raise RuntimeError(
                        "accepted Graph send could not settle its exact pending claim"
                    )
                print(f"    ✅ Successfully sent pending response!")
                operation_states.append(
                    _pending_response_operation_state("healthy", recipient=recipient)
                )
                _fs, user_ref, _thread_ref, _pending_ref = (
                    _pending_claim_refs(
                        user_id,
                        doc,
                        data,
                        graph_send_capability,
                    )
                )
                obligation_ref, _obligation_payload = (
                    _pending_completion_side_document(
                        user_id,
                        user_ref,
                        doc,
                        data,
                        permit_id=graph_send_capability.permit_id,
                        permit_immutable_hash=(
                            graph_send_capability.immutable_hash
                        ),
                        sent_evidence=exact_sent_evidence,
                    )
                )
                operation_states.append(
                    _replay_pending_completion_obligation(
                        user_id,
                        obligation_ref.get(),
                        processing_module,
                    )
                )
            else:
                send_outcome = get_reply_send_outcome()
                failure_reason = (
                    getattr(send_outcome, "error", None)
                    or "send_reply_in_thread returned False"
                )
                if (
                    getattr(send_outcome, "outcome", None)
                    == "draft_mutation_needs_reconciliation"
                ):
                    _cas_pending_draft_review(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        graph_send_capability,
                        failure_reason,
                    )
                    print(
                        "    ⚠️ Prepared Graph reply draft retained for "
                        "authoritative manual review"
                    )
                    continue
                sent_but_unindexed = bool(
                    getattr(send_outcome, "sent_but_unindexed", False)
                    or getattr(send_outcome, "outcome", None) == "sent_but_unindexed"
                )
                suppression_kind = getattr(
                    send_outcome, "campaign_suppression_kind", None
                )
                local_decision = getattr(send_outcome, "campaign_decision", None)
                if suppression_kind in {"maintenance", "unknown"}:
                    decision = local_decision or get_client_automation_decision(
                        user_id, data.get("clientId")
                    )
                    _cas_pending_update(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        {
                            "status": "queued",
                            "processingBy": None,
                            "processingAt": None,
                            "processingLeaseUntil": None,
                            "automationSuppressedState": decision.state,
                            "automationSuppressedReason": decision.reason,
                            "automationSuppressedAt": SERVER_TIMESTAMP,
                            "updatedAt": SERVER_TIMESTAMP,
                        },
                        capability=graph_send_capability,
                        permit_settlement="settled_definitely_not_sent",
                    )
                    print("    ⏸️ Campaign changed during retry; pending response preserved")
                    continue
                if suppression_kind == "terminal":
                    _cas_pending_dead_letter(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        f"Client campaign stopped during retry; pending reply canceled: {failure_reason}",
                        capability=graph_send_capability,
                        permit_settlement="settled_definitely_not_sent",
                    )
                    continue
                if getattr(send_outcome, "outcome", None) in {
                    "accepted_needs_reconciliation",
                    "graph_permit_needs_reconciliation",
                }:
                    _cas_pending_ambiguity(
                        user_id,
                        doc,
                        data,
                        pending_send_claim,
                        graph_send_capability,
                        failure_reason,
                    )
                    print(
                        "    ⚠️ Provider send is unconfirmed; exact pending "
                        "issuer retained for immutable-ID reconciliation"
                    )
                    continue
                if sent_but_unindexed:
                    exact_sent_evidence = getattr(
                        send_outcome, "exact_sent_evidence", None
                    )
                    if isinstance(exact_sent_evidence, dict):
                        _cas_pending_success(
                            user_id,
                            doc,
                            data,
                            pending_send_claim,
                            graph_send_capability,
                            exact_sent_evidence,
                        )
                        print(
                            "    ⚠️ Exact immutable Sent copy was confirmed but "
                            "local indexing failed; send settled without retry"
                        )
                    else:
                        _cas_pending_ambiguity(
                            user_id,
                            doc,
                            data,
                            pending_send_claim,
                            graph_send_capability,
                            failure_reason,
                        )
                        print(
                            "    ⚠️ Provider acceptance is unconfirmed; exact "
                            "pending issuer retained for reconciliation"
                        )
                    continue
                # Update attempt count
                _cas_pending_update(
                    user_id,
                    doc,
                    data,
                    pending_send_claim,
                    {
                        "attempts": attempts + 1,
                        "lastError": failure_reason,
                        "status": "queued",
                        "processingBy": None,
                        "processingAt": None,
                        "processingLeaseUntil": None,
                        "updatedAt": SERVER_TIMESTAMP,
                    },
                    capability=graph_send_capability,
                    permit_settlement="settled_definitely_not_sent",
                )
                print(f"    ❌ Still failing, will retry later")
                # Swallowed per-item Graph send failure -> surface to the health rail.
                operation_states.append(
                    _pending_response_operation_state(
                        "error", recipient=recipient, error=failure_reason
                    )
                )

        except Exception as e:
            error_msg = str(e)
            if pending_send_claim:
                try:
                    if graph_send_capability is not None:
                        send_outcome = get_reply_send_outcome()
                        if (
                            getattr(send_outcome, "outcome", None)
                            != "draft_mutation_needs_reconciliation"
                        ):
                            try:
                                resolve_graph_send_permit(
                                    graph_send_capability,
                                    "needs_reconciliation",
                                    evidence={
                                        "reason": error_msg[:1500],
                                        "phase": "pending_worker_exception",
                                    },
                                )
                            except GraphSendPermitBlocked:
                                pass
                            _cas_pending_ambiguity(
                                user_id,
                                doc,
                                data,
                                pending_send_claim,
                                graph_send_capability,
                                "Pending Graph permit requires reconciliation after "
                                f"worker error: {error_msg}",
                            )
                    else:
                        _cas_pending_update(
                            user_id,
                            doc,
                            data,
                            pending_send_claim,
                            {
                                "attempts": attempts + 1,
                                "lastError": error_msg,
                                "status": "queued",
                                "processingBy": None,
                                "processingAt": None,
                                "processingLeaseUntil": None,
                                "updatedAt": SERVER_TIMESTAMP,
                            },
                        )
                except Exception as cas_error:
                    error_msg = f"{error_msg}; exact pending CAS failed: {cas_error}"
            print(f"    ❌ Error: {error_msg[:50]}...")
            operation_states.append(
                _pending_response_operation_state(
                    "error", recipient=recipient, error=error_msg
                )
            )

    return operation_states


def clear_pending_response(user_id: str, thread_id: str) -> bool:
    """
    Remove a pending response (called after successful manual send or when no longer needed).
    """
    from .clients import _fs

    doc_ref = _fs.collection("users").document(user_id).collection("pendingResponses").document(thread_id)
    doc = doc_ref.get()

    if doc.exists:
        doc_ref.delete()
        return True

    return False
