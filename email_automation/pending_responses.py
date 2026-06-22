"""
Pending Responses Queue

Handles retry logic for failed AI-generated response emails.
Similar to outbox retry, but for responses that fail to send after processing.
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any
from google.cloud.firestore import SERVER_TIMESTAMP

# Maximum retry attempts before giving up
MAX_RESPONSE_ATTEMPTS = 5


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

    _fs.collection("users").document(user_id).collection("deadLetterQueue").add(payload)


def queue_pending_response(
    user_id: str,
    thread_id: str,
    msg_id: str,
    recipient: str,
    response_body: str,
    client_id: Optional[str] = None,
    error: Optional[str] = None
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


def get_pending_responses(user_id: str) -> list:
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


def process_pending_responses(user_id: str, headers: Dict[str, str]) -> int:
    """
    Retry sending all pending responses.

    Returns the number of successfully sent responses.
    """
    from .processing import send_reply_in_thread

    pending = get_pending_responses(user_id)

    if not pending:
        return 0

    print(f"\n📬 Found {len(pending)} pending response(s) to retry")

    success_count = 0
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
                success_count += 1
            else:
                failure_reason = (
                    getattr(send_reply_in_thread, "last_error", None)
                    or "send_reply_in_thread returned False"
                )
                sent_but_unindexed = (
                    getattr(send_reply_in_thread, "sent_but_unindexed", False)
                    or getattr(send_reply_in_thread, "last_outcome", None) == "sent_but_unindexed"
                )
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

        except Exception as e:
            error_msg = str(e)
            doc.reference.update({
                "attempts": attempts + 1,
                "lastError": error_msg,
                "updatedAt": SERVER_TIMESTAMP,
            })
            print(f"    ❌ Error: {error_msg[:50]}...")

    return success_count


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
