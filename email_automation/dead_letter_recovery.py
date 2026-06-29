"""Guarded dead-letter recovery helpers.

Dead-letter items are intentionally conservative: they often exist because a
send may have happened but the system could not safely prove/index it. Recovery
must never bypass the Sent Items duplicate-send guards.
"""

from typing import Any, Dict, Optional

from google.cloud.firestore import SERVER_TIMESTAMP

from .sent_mail_guard import (
    SentMailGuardLookupError,
    find_matching_sent_message_for_retry,
    find_sent_conversation_continuation_for_retry,
    sent_after_from_retry_data,
)


RESOLUTION_ACTIONS = {
    "mark_reconciled": "reconciled",
    "acknowledge": "acknowledged",
    "discard": "discarded",
}

RECOVERY_METADATA_KEYS = {
    "alreadySent",
    "deadLetteredAt",
    "failedAt",
    "failureReason",
    "lastError",
    "maxAttempts",
    "movedAt",
    "originalDocId",
    "recoveredAt",
    "recoveredBy",
    "recoveryNote",
    "recoveryStatus",
    "resolution",
    "resolvedAt",
    "resolvedBy",
    "sentDateTime",
    "sentMessageId",
    "sentMessageIds",
    "sentRecipients",
    "source",
}


def _dead_letter_ref(user_id: str, dead_letter_id: str):
    from .clients import _fs

    return (
        _fs.collection("users")
        .document(user_id)
        .collection("deadLetterQueue")
        .document(dead_letter_id)
    )


def _user_collection(user_id: str, collection_name: str):
    from .clients import _fs

    return _fs.collection("users").document(user_id).collection(collection_name)


def _doc_ref_from_add_result(add_result):
    if isinstance(add_result, tuple) and len(add_result) >= 2:
        return add_result[1]
    return add_result


def _recipient_from_dead_letter(data: Dict[str, Any]) -> str:
    for value in data.get("assignedEmails") or data.get("sentRecipients") or []:
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("recipient", "email", "brokerEmail"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _body_from_dead_letter(data: Dict[str, Any]) -> str:
    for key in ("script", "responseBody", "body", "messageBody"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _is_manual_continuation_dead_letter(data: Dict[str, Any]) -> bool:
    if data.get("manualContinuation"):
        return True
    reason = " ".join(str(data.get(key) or "") for key in ("failureReason", "lastError", "reason"))
    return "manually continued" in reason.lower()


def _mark_dead_letter(ref, status: str, *, operator_id: Optional[str], note: Optional[str] = None, **extra):
    payload = {
        "recoveryStatus": status,
        "updatedAt": SERVER_TIMESTAMP,
        **extra,
    }
    if operator_id:
        payload["recoveredBy"] = operator_id
    if note:
        payload["recoveryNote"] = note
    ref.update(payload)


def _identity_update_from_sent_match(sent_match: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "status": "needs_reconciliation",
        "alreadySent": True,
        "recoveryStatus": "already_sent",
        "updatedAt": SERVER_TIMESTAMP,
    }
    for key in ("sentMessageId", "internetMessageId", "conversationId", "sentDateTime"):
        value = sent_match.get(key) or (sent_match.get("id") if key == "sentMessageId" else None)
        if value:
            payload[key] = value
    return payload


def _safe_outbox_payload(data: Dict[str, Any], dead_letter_id: str, operator_id: Optional[str], note: Optional[str]):
    outbox_payload = {
        key: value
        for key, value in data.items()
        if key not in RECOVERY_METADATA_KEYS
    }
    outbox_payload.update({
        "attempts": 0,
        "status": "queued",
        "requiresSentItemsPreflight": True,
        "processingBy": None,
        "processingAt": None,
        "lastSendAttemptAt": None,
        "recoveryFromDeadLetterId": dead_letter_id,
        "recoveredAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    })
    if operator_id:
        outbox_payload["recoveredBy"] = operator_id
    if note:
        outbox_payload["recoveryNote"] = note
    return outbox_payload


def _update_requeued_action_audit(
    user_id: str,
    data: Dict[str, Any],
    outbox_id: Optional[str],
    operator_id: Optional[str],
    note: Optional[str],
) -> None:
    action_audit_id = data.get("actionAuditId")
    if not action_audit_id:
        return
    payload = {
        "status": "queued",
        "outboxId": outbox_id,
        "recoveryStatus": "requeued",
        "updatedAt": SERVER_TIMESTAMP,
    }
    if operator_id:
        payload["recoveredBy"] = operator_id
    if note:
        payload["recoveryNote"] = note
    try:
        _user_collection(user_id, "actionAudit").document(action_audit_id).set(payload, merge=True)
    except Exception as exc:
        print(f"   ⚠️ Could not update action audit {action_audit_id} after dead-letter recovery: {exc}")


def _resolve_without_requeue(
    user_id: str,
    ref,
    data: Dict[str, Any],
    action: str,
    operator_id: Optional[str],
    note: Optional[str],
) -> Dict[str, Any]:
    resolution = RESOLUTION_ACTIONS[action]
    update_payload = {
        "status": resolution,
        "resolution": action,
        "resolvedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    if operator_id:
        update_payload["resolvedBy"] = operator_id
    if note:
        update_payload["resolutionNote"] = note
    ref.update(update_payload)
    if data.get("actionAuditId"):
        _user_collection(user_id, "actionAudit").document(data["actionAuditId"]).set({
            "status": resolution,
            "resolution": action,
            "updatedAt": SERVER_TIMESTAMP,
        }, merge=True)
    return {"success": True, "code": resolution}


def _requeue_verified_unsent(
    user_id: str,
    dead_letter_id: str,
    ref,
    data: Dict[str, Any],
    headers: Optional[Dict[str, str]],
    operator_id: Optional[str],
    note: Optional[str],
) -> Dict[str, Any]:
    if data.get("alreadySent"):
        _mark_dead_letter(ref, "blocked_already_sent", operator_id=operator_id, note=note)
        return {"success": False, "code": "unsafe_already_sent"}

    if _is_manual_continuation_dead_letter(data):
        _mark_dead_letter(ref, "blocked_manual_continuation", operator_id=operator_id, note=note)
        return {"success": False, "code": "blocked_manual_continuation"}

    if data.get("source") != "outbox":
        _mark_dead_letter(ref, "blocked_unsupported_source", operator_id=operator_id, note=note)
        return {"success": False, "code": "unsupported_source"}

    if not headers:
        _mark_dead_letter(ref, "blocked_missing_headers", operator_id=operator_id, note=note)
        return {"success": False, "code": "missing_headers"}

    recipient = _recipient_from_dead_letter(data)
    body = _body_from_dead_letter(data)
    if not recipient or not body:
        _mark_dead_letter(ref, "blocked_missing_send_identity", operator_id=operator_id, note=note)
        return {"success": False, "code": "missing_send_identity"}

    try:
        sent_match = find_matching_sent_message_for_retry(
            headers,
            recipient=recipient,
            body=body,
            subject=data.get("subject"),
            conversation_id=data.get("conversationId") or data.get("sourceConversationId"),
            sent_after=sent_after_from_retry_data(data),
        )
        if sent_match:
            ref.update(_identity_update_from_sent_match(sent_match))
            return {"success": False, "code": "already_sent"}

        manual_continuation = find_sent_conversation_continuation_for_retry(
            headers,
            conversation_id=data.get("conversationId") or data.get("sourceConversationId"),
            sent_after=sent_after_from_retry_data(data),
        )
    except SentMailGuardLookupError as exc:
        _mark_dead_letter(
            ref,
            "blocked_guard_unreadable",
            operator_id=operator_id,
            note=note,
            recoveryError=str(exc),
        )
        return {"success": False, "code": "guard_unreadable"}

    if manual_continuation:
        _mark_dead_letter(
            ref,
            "blocked_manual_continuation",
            operator_id=operator_id,
            note=note,
            manualContinuation=manual_continuation,
        )
        return {"success": False, "code": "blocked_manual_continuation"}

    add_result = _user_collection(user_id, "outbox").add(
        _safe_outbox_payload(data, dead_letter_id, operator_id, note)
    )
    outbox_ref = _doc_ref_from_add_result(add_result)
    outbox_id = getattr(outbox_ref, "id", None)
    ref.update({
        "status": "requeued",
        "recoveryStatus": "requeued",
        "requeuedOutboxId": outbox_id,
        "resolvedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    })
    _update_requeued_action_audit(user_id, data, outbox_id, operator_id, note)
    return {"success": True, "code": "requeued", "outboxId": outbox_id}


def resolve_dead_letter_item(
    user_id: str,
    dead_letter_id: str,
    *,
    action: str,
    headers: Optional[Dict[str, str]] = None,
    operator_id: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a single dead-letter item without risking a duplicate send."""
    ref = _dead_letter_ref(user_id, dead_letter_id)
    snapshot = ref.get()
    if not snapshot.exists:
        return {"success": False, "code": "not_found"}

    data = snapshot.to_dict() or {}
    if action in RESOLUTION_ACTIONS:
        return _resolve_without_requeue(user_id, ref, data, action, operator_id, note)
    if action == "requeue_verified_unsent":
        return _requeue_verified_unsent(user_id, dead_letter_id, ref, data, headers, operator_id, note)

    return {"success": False, "code": "unsupported_action"}
