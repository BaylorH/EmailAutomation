"""
Pending Responses Queue

Handles retry logic for failed AI-generated response emails.
Similar to outbox retry, but for responses that fail to send after processing.
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from google.cloud.firestore import SERVER_TIMESTAMP

from .sent_mail_guard import (
    SentMailGuardLookupError,
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

# Maximum retry attempts before giving up
MAX_RESPONSE_ATTEMPTS = 5


def _preserve_pending_campaign_suppression(doc, decision) -> None:
    doc.reference.update({
        "status": "queued",
        "processingBy": None,
        "processingAt": None,
        "automationSuppressedState": decision.state,
        "automationSuppressedReason": decision.reason,
        "automationSuppressedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    })


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

    pending = get_pending_responses(user_id, apply_send_gates=False)

    if not pending:
        return operation_states

    print(f"\n📬 Found {len(pending)} pending response(s) to retry")

    for item in pending:
        doc = item["doc"]
        data = item["data"]

        thread_id = data.get("threadId")
        msg_id = data.get("msgId")
        recipient = data.get("recipient")
        response_body = data.get("responseBody")
        attempts = data.get("attempts", 0)

        print(f"  → Retrying response to {recipient} (attempt {attempts + 1}/{MAX_RESPONSE_ATTEMPTS})")

        try:
            campaign_decision = get_client_automation_decision(
                user_id,
                data.get("clientId"),
            )
            if _gate_pending_response(
                user_id,
                doc,
                data,
                decision=campaign_decision,
            ):
                print("    ⏸️ Pending response suppressed by current campaign state")
                continue

            if attempts >= MAX_RESPONSE_ATTEMPTS:
                reason = data.get("lastError") or f"Exceeded max attempts ({MAX_RESPONSE_ATTEMPTS})"
                _move_pending_response_to_dead_letter(user_id, doc, data, reason)
                print(f"    ☠️ Pending response exceeded max attempts ({MAX_RESPONSE_ATTEMPTS})")
                continue

            body_validation = validate_outbound_body(response_body)
            if not body_validation.is_safe:
                _move_pending_response_to_dead_letter(
                    user_id,
                    doc,
                    data,
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
                    _move_pending_response_to_dead_letter(
                        user_id,
                        doc,
                        data,
                        f"Sent Items retry guard could not verify prior send; manual review required before retry: {exc}",
                    )
                    print("    ⚠️ Sent Items retry guard failed closed; moved pending response to manual review")
                    continue
                if sent_match:
                    record_sent_unindexed_response(
                        user_id,
                        thread_id,
                        msg_id,
                        recipient,
                        response_body,
                        data.get("clientId"),
                        "Prior failed attempt appears already sent in Sent Items; stopped before retry",
                        source_context="pendingResponses",
                        original_doc_id=doc.id,
                        sent_match=sent_match,
                    )
                    doc.reference.delete()
                    print("    ⚠️ Prior send found in Sent Items; moved to reconciliation without retrying")
                    continue
                try:
                    manual_continuation = find_sent_conversation_continuation_for_retry(
                        headers,
                        conversation_id=data.get("conversationId"),
                        sent_after=sent_after_from_retry_data(data),
                    )
                except SentMailGuardLookupError as exc:
                    _move_pending_response_to_dead_letter(
                        user_id,
                        doc,
                        data,
                        f"Sent Items retry guard could not verify manual continuation before retry; manual review required: {exc}",
                    )
                    print("    ⚠️ Manual continuation guard failed closed; moved pending response to manual review")
                    continue
                if manual_continuation:
                    _move_pending_response_to_dead_letter(
                        user_id,
                        doc,
                        data,
                        "Pending response stopped because Sent Items shows the user manually continued this conversation; review before retrying the stale draft.",
                    )
                    print("    ⚠️ Manual continuation found in Sent Items; moved pending response to manual review")
                    continue

            reset_reply_send_outcome()
            sent = send_reply_in_thread(
                user_id=user_id,
                headers=headers,
                body=response_body,
                current_msg_id=msg_id,
                recipient=recipient,
                thread_id=thread_id
            )

            if sent:
                print(f"    ✅ Successfully sent pending response!")
                doc.reference.delete()
                operation_states.append(
                    _pending_response_operation_state("healthy", recipient=recipient)
                )
            else:
                send_outcome = get_reply_send_outcome()
                failure_reason = (
                    getattr(send_outcome, "error", None)
                    or "send_reply_in_thread returned False"
                )
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
                    _preserve_pending_campaign_suppression(doc, decision)
                    print("    ⏸️ Campaign changed during retry; pending response preserved")
                    continue
                if suppression_kind == "terminal":
                    _move_pending_response_to_dead_letter(
                        user_id,
                        doc,
                        data,
                        f"Client campaign stopped during retry; pending reply canceled: {failure_reason}",
                    )
                    continue
                if sent_but_unindexed:
                    record_sent_unindexed_response(
                        user_id,
                        thread_id,
                        msg_id,
                        recipient,
                        response_body,
                        data.get("clientId"),
                        failure_reason,
                        source_context="pendingResponses",
                        original_doc_id=doc.id,
                    )
                    doc.reference.delete()
                    print("    ⚠️ Reply may have sent but could not be indexed; moved to reconciliation instead of retrying")
                    continue
                # Update attempt count
                doc.reference.update({
                    "attempts": attempts + 1,
                    "lastError": failure_reason,
                    "updatedAt": SERVER_TIMESTAMP,
                })
                print(f"    ❌ Still failing, will retry later")
                # Swallowed per-item Graph send failure -> surface to the health rail.
                operation_states.append(
                    _pending_response_operation_state(
                        "error", recipient=recipient, error=failure_reason
                    )
                )

        except Exception as e:
            error_msg = str(e)
            doc.reference.update({
                "attempts": attempts + 1,
                "lastError": error_msg,
                "updatedAt": SERVER_TIMESTAMP,
            })
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
