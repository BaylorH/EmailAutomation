"""
Automatic Follow-Up Email System
================================

This module handles automatic follow-up emails when brokers don't respond
within configurable time periods.

Key features:
- 0-3 configurable follow-ups per thread
- Hours or days timing
- Pause/resume when broker responds then goes silent
- Sends as replies to maintain thread continuity

Called from main.py after inbox scanning.
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from google.cloud.firestore import SERVER_TIMESTAMP

from .clients import _fs
from .utils import (
    exponential_backoff_request,
    format_email_body_with_footer,
    get_signature_attachments,
    needs_signature_attachments,
    safe_preview,
    resolve_signature_settings,
)
from .messaging import save_message
from .sent_mail_guard import (
    SentMailGuardLookupError,
    find_matching_sent_message_for_retry,
    sent_after_from_retry_data,
)

# Claim timeout for follow-up processing (prevent duplicate sends)
FOLLOWUP_CLAIM_TIMEOUT_SECONDS = 60
SYNTHETIC_OUTBOUND_SOURCES = {"dashboard_outbox_reply", "followup_scheduler"}


def _claim_followup(user_id: str, thread_id: str, current_index: int) -> bool:
    """
    Atomically claim a follow-up for processing to prevent duplicate sends.

    Uses a transaction to check that:
    1. No other process is currently sending this follow-up
    2. The current index hasn't changed since we read it

    Returns True if successfully claimed, False if already being processed.
    """
    from google.cloud.firestore import transactional

    thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)

    @transactional
    def claim_transaction(transaction, thread_ref, expected_index):
        snapshot = thread_ref.get(transaction=transaction)
        if not snapshot.exists:
            return False

        data = snapshot.to_dict() or {}
        followup_config = data.get("followUpConfig", {})

        # Check if index has changed (another process already sent)
        actual_index = followup_config.get("currentFollowUpIndex", 0)
        if actual_index != expected_index:
            print(f"   ⏭️ Follow-up index changed ({expected_index} → {actual_index}), skipping")
            return False

        # Check if already being processed
        processing_by = followup_config.get("processingBy")
        processing_at = followup_config.get("processingAt")

        now = datetime.now(timezone.utc)

        if processing_by and processing_at:
            if hasattr(processing_at, 'timestamp'):
                claim_age = (now - processing_at.replace(tzinfo=timezone.utc)).total_seconds()
            else:
                claim_age = (now - processing_at).total_seconds()

            if claim_age < FOLLOWUP_CLAIM_TIMEOUT_SECONDS:
                print(f"   ⏭️ Follow-up already being processed by {processing_by} ({int(claim_age)}s ago)")
                return False

        # Claim the follow-up
        import socket
        worker_id = f"followup-{socket.gethostname()[:20]}"
        transaction.update(thread_ref, {
            "followUpConfig.processingBy": worker_id,
            "followUpConfig.processingAt": now
        })
        return True

    try:
        transaction = _fs.transaction()
        return claim_transaction(transaction, thread_ref, current_index)
    except Exception as e:
        print(f"   ⚠️ Failed to claim follow-up for {thread_id[:20]}...: {e}")
        return False


def _release_followup_claim(
    user_id: str,
    thread_id: str,
    *,
    reason: Optional[str] = None,
    attempted_at: Optional[datetime] = None,
    current_index: Optional[int] = None,
    fail_closed: bool = False,
):
    """Release claim on a follow-up (called on failure to allow retry)."""
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        update_payload = {
            "followUpConfig.processingBy": None,
            "followUpConfig.processingAt": None,
        }
        if reason:
            update_payload["followUpConfig.lastSendError"] = reason
        if attempted_at:
            update_payload["followUpConfig.lastSendAttemptAt"] = attempted_at
        if current_index is not None:
            update_payload["followUpConfig.lastSendAttemptIndex"] = current_index
        if fail_closed:
            update_payload.update({
                "followUpStatus": "needs_review",
                "status": "action_needed",
                "statusReason": "followup_send_guard_failed",
                "followUpConfig.enabled": False,
                "followUpConfig.nextFollowUpAt": None,
            })
        thread_ref.update(update_payload)
    except Exception as e:
        print(f"   ⚠️ Failed to release follow-up claim: {e}")


def _save_followup_message(
    user_id: str,
    thread_id: str,
    recipient: str,
    subject: str,
    body: str,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
) -> bool:
    """Persist a sent follow-up into thread history for dashboard reconciliation."""
    try:
        synthetic_id = f"followup-{thread_id}-{int(time.time() * 1000)}"
        html_body = format_email_body_with_footer(
            body,
            user_signature,
            signature_mode,
            user_email=user_email,
        )
        return save_message(
            user_id,
            thread_id,
            synthetic_id,
            {
                "direction": "outbound",
                "from": "me",
                "to": [recipient] if recipient else [],
                "subject": subject,
                "body": html_body,
                "bodyPreview": safe_preview(body, 300),
                "sentDateTime": datetime.now(timezone.utc).isoformat(),
                "headers": {"internetMessageId": synthetic_id},
                "source": "followup_scheduler",
            },
        )
    except Exception as e:
        print(f"   ⚠️ Could not save follow-up message for {thread_id[:20]}...: {e}")
        return False


def _clear_followup_row_highlight(user_id: str, thread_id: str) -> bool:
    """Clear Sheet highlight when a follow-up sequence reaches a terminal state."""
    try:
        from .clients import _get_sheet_id_or_fail
        from .sheets import clear_row_highlight

        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()
        if not thread_doc.exists:
            return False
        thread_data = thread_doc.to_dict() or {}
        client_id = thread_data.get("clientId")
        row_number = thread_data.get("rowNumber")
        if not client_id or not row_number:
            return False
        sheet_id = _get_sheet_id_or_fail(user_id, client_id)
        return clear_row_highlight(sheet_id, row_number)
    except Exception as e:
        print(f"   ⚠️ Could not clear terminal follow-up row highlight for {thread_id[:20]}...: {e}")
        return False


def _is_graph_backed_outbound_message(message_data: Dict[str, Any]) -> bool:
    """True when an outbound history entry can be found again through Microsoft Graph."""
    if (message_data or {}).get("source") in SYNTHETIC_OUTBOUND_SOURCES:
        return False

    internet_msg_id = ((message_data or {}).get("headers") or {}).get("internetMessageId")
    if not internet_msg_id:
        return False

    return not str(internet_msg_id).startswith(("dashboard-reply-", "followup-"))


def _select_reply_anchor_message(outbound_message_docs: List[Any]) -> Optional[Dict[str, Any]]:
    """Pick the newest outbound message that has a real Graph internetMessageId."""
    for doc in outbound_message_docs:
        data = doc.to_dict() or {}
        if _is_graph_backed_outbound_message(data):
            return data
    return None


def check_and_send_followups(user_id: str, headers: Dict[str, str]) -> int:
    """
    Main entry point: scan threads needing follow-ups and send them.

    Called from main.py every 30 minutes.

    Returns: Number of follow-ups sent
    """
    print(f"\n{'='*60}")
    print("FOLLOW-UP CHECK")
    print(f"{'='*60}")

    now = datetime.now(timezone.utc)
    followups_sent = 0

    # Query threads with active follow-up tracking
    threads_ref = _fs.collection("users").document(user_id).collection("threads")

    # Find threads that are waiting for follow-up
    # Status must be 'waiting' and nextFollowUpAt must be in the past
    try:
        query = threads_ref.where("followUpStatus", "==", "waiting")
        waiting_threads = list(query.stream())
    except Exception as e:
        print(f"   Error querying follow-up threads: {e}")
        return 0

    if not waiting_threads:
        print("   No threads waiting for follow-up")
        return 0

    print(f"   Found {len(waiting_threads)} threads with follow-up tracking")
    total_threads = len(waiting_threads)

    for idx, thread_doc in enumerate(waiting_threads):
        thread_data = thread_doc.to_dict()
        thread_id = thread_doc.id

        followup_config = thread_data.get("followUpConfig", {})

        if not followup_config.get("enabled", False):
            continue

        next_followup_at = followup_config.get("nextFollowUpAt")
        if not next_followup_at:
            continue

        # Convert Firestore timestamp to datetime
        if hasattr(next_followup_at, 'timestamp'):
            next_followup_dt = datetime.fromtimestamp(
                next_followup_at.timestamp(),
                tz=timezone.utc
            )
        else:
            continue

        # Check if it's time for follow-up
        if now < next_followup_dt:
            time_remaining = next_followup_dt - now
            print(f"   Thread {thread_id[:20]}... - {time_remaining} until follow-up")
            continue

        # Check if broker has responded
        if thread_data.get("hasInboundReply", False):
            # Broker responded - pause the follow-up sequence
            _pause_followup(user_id, thread_id)
            continue

        # Get current follow-up index and messages
        current_index = followup_config.get("currentFollowUpIndex", 0)
        followups = followup_config.get("followUps", [])

        if current_index >= len(followups):
            # All follow-ups exhausted
            _mark_followup_complete(user_id, thread_id, "max_reached")
            continue

        # Claim the follow-up to prevent duplicate sends
        if not _claim_followup(user_id, thread_id, current_index):
            continue

        # Send the follow-up
        success = _send_followup_email(
            user_id=user_id,
            headers=headers,
            thread_id=thread_id,
            thread_data=thread_data,
            followup_config=followup_config,
            followup_index=current_index
        )

        if success:
            followups_sent += 1

            # Schedule next follow-up if there are more
            _schedule_next_followup(
                user_id=user_id,
                thread_id=thread_id,
                followup_config=followup_config,
                just_sent_index=current_index
            )

            # Stagger follow-up sends by 2 minutes to avoid spam detection
            # Only sleep if there are more threads to process
            remaining_threads = total_threads - (idx + 1)
            if remaining_threads > 0:
                print(f"   ⏳ Waiting 2 minutes before next follow-up ({remaining_threads} remaining)...")
                time.sleep(120)  # 2 minutes
        else:
            # Release the claim so it can be retried
            _release_followup_claim(
                user_id,
                thread_id,
                reason=getattr(_send_followup_email, "last_error", None),
                attempted_at=getattr(_send_followup_email, "last_attempt_at", None),
                current_index=current_index,
                fail_closed=getattr(_send_followup_email, "guard_failed_closed", False),
            )

    print(f"\n   Sent {followups_sent} follow-up email(s)")
    return followups_sent


def _send_followup_email(
    user_id: str,
    headers: Dict[str, str],
    thread_id: str,
    thread_data: Dict,
    followup_config: Dict,
    followup_index: int
) -> bool:
    """Send a follow-up email for a specific thread."""
    import requests

    _send_followup_email.last_error = None
    _send_followup_email.last_attempt_at = None
    _send_followup_email.guard_failed_closed = False

    try:
        followups = followup_config.get("followUps", [])
        if followup_index >= len(followups):
            return False

        followup = followups[followup_index]
        followup_message = followup.get("message", "")

        if not followup_message:
            followup_message = _get_default_followup_message(followup_index)

        recipient_emails = thread_data.get("email", [])
        if not recipient_emails:
            print(f"   No recipient email for thread {thread_id[:20]}...")
            return False

        recipient = recipient_emails[0] if isinstance(recipient_emails, list) else recipient_emails

        # Get the last outbound message to reply to
        messages_ref = (_fs.collection("users").document(user_id)
                       .collection("threads").document(thread_id)
                       .collection("messages"))

        try:
            outbound_messages = list(
                messages_ref.where("direction", "==", "outbound")
                .order_by("sentDateTime", direction="DESCENDING")
                .limit(10)
                .stream()
            )
        except Exception as e:
            # Index might not exist, try without order_by
            outbound_messages = [
                doc for doc in messages_ref.stream()
                if doc.to_dict().get("direction") == "outbound"
            ]
            if outbound_messages:
                outbound_messages.sort(
                    key=lambda doc: (doc.to_dict() or {}).get("sentDateTime", ""),
                    reverse=True
                )

        if not outbound_messages:
            print(f"   No outbound messages found in thread {thread_id[:20]}...")
            return False

        last_outbound = _select_reply_anchor_message(outbound_messages)
        if not last_outbound:
            print(f"   No Graph-backed outbound message found in thread {thread_id[:20]}...")
            return False

        internet_msg_id = last_outbound.get("headers", {}).get("internetMessageId")

        # Find the Graph message ID
        base = "https://graph.microsoft.com/v1.0"

        if internet_msg_id:
            # Search by internetMessageId
            search_resp = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages",
                    headers=headers,
                    params={
                        "$filter": f"internetMessageId eq '{internet_msg_id}'",
                        "$select": "id,subject,conversationId"
                    },
                    timeout=30
                )
            )

            if search_resp.status_code != 200:
                print(f"   Failed to find message: {search_resp.status_code}")
                return False

            messages = search_resp.json().get("value", [])
            if not messages:
                print(f"   Message not found in mailbox")
                return False

            graph_msg_id = messages[0]["id"]
            subject = messages[0].get("subject", thread_data.get("subject", "Follow-up"))
            conversation_id = messages[0].get("conversationId")
        else:
            print(f"   No internetMessageId for reply")
            return False

        # Personalize the message with contact name if available
        contact_name = thread_data.get("contactName", "")

        # Fallback: fetch contact name from sheet if not on thread
        if not contact_name and "[NAME]" in followup_message:
            try:
                from .clients import _get_sheet_id_or_fail, _sheets_client
                client_id = thread_data.get("clientId")
                row_number = thread_data.get("rowNumber")
                if client_id and row_number:
                    sheet_id = _get_sheet_id_or_fail(user_id, client_id)
                    sheets = _sheets_client()
                    # Fetch the row to get Leasing Contact (column E = index 4)
                    result = sheets.spreadsheets().values().get(
                        spreadsheetId=sheet_id,
                        range=f"A{row_number}:F{row_number}"
                    ).execute()
                    row_values = result.get("values", [[]])[0]
                    if len(row_values) >= 5:
                        contact_name = row_values[4]  # Leasing Contact column (E)
                        print(f"   Fetched contact name from sheet: {contact_name}")
            except Exception as e:
                print(f"   Could not fetch contact name from sheet: {e}")

        if contact_name and "[NAME]" in followup_message:
            first_name = contact_name.split()[0] if contact_name else ""
            followup_message = followup_message.replace("[NAME]", first_name)

        # Get user's signature settings
        user_doc = _fs.collection("users").document(user_id).get()
        user_signature = None
        signature_mode = None
        user_email = None
        if user_doc.exists:
            user_data = user_doc.to_dict() or {}
            user_signature, signature_mode, user_email = resolve_signature_settings(user_data)

        # Format as HTML with signature
        html_content = format_email_body_with_footer(
            followup_message,
            user_signature,
            signature_mode,
            user_email=user_email,
        )

        last_attempt_index = followup_config.get("lastSendAttemptIndex")
        retry_state_matches_current_followup = (
            last_attempt_index is None or last_attempt_index == followup_index
        )
        if retry_state_matches_current_followup and (
            followup_config.get("lastSendError") or followup_config.get("lastSendAttemptAt")
        ):
            try:
                sent_match = find_matching_sent_message_for_retry(
                    headers,
                    recipient=recipient,
                    body=followup_message,
                    subject=subject,
                    conversation_id=conversation_id,
                    sent_after=sent_after_from_retry_data(followup_config),
                )
            except SentMailGuardLookupError as exc:
                _send_followup_email.last_error = f"Sent Items retry guard failed: {exc}"
                _send_followup_email.guard_failed_closed = True
                print(f"   ⚠️ {_send_followup_email.last_error}")
                return False

            if sent_match:
                print(f"   ⚠️ Prior follow-up send found in Sent Items; recording without resending")
                _save_followup_message(
                    user_id, thread_id, recipient, subject,
                    followup_message, user_signature, signature_mode, user_email
                )
                _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
                    "lastOutboundAt": SERVER_TIMESTAMP,
                    "updatedAt": SERVER_TIMESTAMP,
                    "followUpConfig.lastFollowUpSentAt": SERVER_TIMESTAMP,
                    "followUpConfig.lastSendError": None,
                    "followUpConfig.lastSendAttemptAt": sent_match.get("sentDateTime"),
                })
                return True

        # Send as reply with signature attachments
        send_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        _send_followup_email.last_attempt_at = send_attempt_at
        if needs_signature_attachments(signature_mode, user_signature, user_email=user_email):
            # Use createReply to get a draft, add attachments, then send
            create_reply_resp = exponential_backoff_request(
                lambda: requests.post(f"{base}/me/messages/{graph_msg_id}/createReply", headers=headers, timeout=30)
            )
            reply_draft = create_reply_resp.json()
            reply_draft_id = reply_draft.get("id")

            # Update draft body AND set correct recipient
            # (createReply on sent messages doesn't auto-populate toRecipients correctly)
            exponential_backoff_request(
                lambda: requests.patch(
                    f"{base}/me/messages/{reply_draft_id}",
                    headers=headers,
                    json={
                        "body": {"contentType": "HTML", "content": html_content},
                        "toRecipients": [{"emailAddress": {"address": recipient}}]
                    },
                    timeout=30
                )
            )

            # Add signature attachments
            signature_attachments = get_signature_attachments(user_signature, signature_mode, user_email=user_email)
            for attachment in signature_attachments:
                try:
                    att_resp = exponential_backoff_request(
                        lambda att=attachment: requests.post(
                            f"{base}/me/messages/{reply_draft_id}/attachments",
                            headers=headers,
                            json=att,
                            timeout=30
                        )
                    )
                    if att_resp.status_code in [200, 201]:
                        print(f"      📎 Attached {attachment['name']}")
                except Exception as e:
                    print(f"      ⚠️ Error attaching {attachment['name']}: {e}")

            # Send the reply
            reply_resp = exponential_backoff_request(
                lambda: requests.post(f"{base}/me/messages/{reply_draft_id}/send", headers=headers, timeout=30),
                max_retries=1,
                operation="graph_send",
            )
        else:
            # No attachments - use simple reply endpoint
            # Must explicitly set toRecipients since we're replying to our own sent message
            reply_body = {
                "message": {
                    "toRecipients": [{"emailAddress": {"address": recipient}}],
                    "body": {
                        "contentType": "HTML",
                        "content": html_content
                    }
                }
            }

            reply_resp = exponential_backoff_request(
                lambda: requests.post(
                    f"{base}/me/messages/{graph_msg_id}/reply",
                    headers=headers,
                    json=reply_body,
                    timeout=30
                ),
                max_retries=1,
                operation="graph_send",
            )

        if reply_resp.status_code in [200, 201, 202]:
            print(f"   Sent follow-up #{followup_index + 1} for thread {thread_id[:20]}...")
            _save_followup_message(
                user_id, thread_id, recipient, subject,
                followup_message, user_signature, signature_mode, user_email
            )

            # Update thread
            _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
                "lastOutboundAt": SERVER_TIMESTAMP,
                "updatedAt": SERVER_TIMESTAMP,
                "followUpConfig.lastFollowUpSentAt": SERVER_TIMESTAMP
            })

            return True
        else:
            print(f"   Failed to send follow-up: {reply_resp.status_code}")
            _send_followup_email.last_error = f"Follow-up Graph send returned HTTP {reply_resp.status_code}"
            return False

    except Exception as e:
        _send_followup_email.last_error = str(e)
        print(f"   Error sending follow-up: {e}")
        return False


def _schedule_next_followup(
    user_id: str,
    thread_id: str,
    followup_config: Dict,
    just_sent_index: int
):
    """Schedule the next follow-up in the sequence."""
    followups = followup_config.get("followUps", [])
    next_index = just_sent_index + 1

    if next_index >= len(followups):
        # No more follow-ups
        _mark_followup_complete(user_id, thread_id, "max_reached")
        return

    # Calculate next follow-up time
    next_followup = followups[next_index]
    wait_time = next_followup.get("waitTime", 3)
    wait_unit = next_followup.get("waitUnit", "days")

    if wait_unit == "minutes":
        delta = timedelta(minutes=wait_time)
    elif wait_unit == "hours":
        delta = timedelta(hours=wait_time)
    else:
        delta = timedelta(days=wait_time)

    next_followup_at = datetime.now(timezone.utc) + delta

    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpConfig.currentFollowUpIndex": next_index,
        "followUpConfig.nextFollowUpAt": next_followup_at,
        "followUpConfig.processingBy": None,
        "followUpConfig.processingAt": None,
        "followUpConfig.lastSendError": None,
        "followUpConfig.lastSendAttemptAt": None,
        "followUpConfig.lastSendAttemptIndex": None,
        "followUpStatus": "waiting",
        "updatedAt": SERVER_TIMESTAMP
    })

    print(f"   Next follow-up scheduled for {next_followup_at.strftime('%Y-%m-%d %H:%M')} UTC")


def schedule_followup_after_auto_response(user_id: str, thread_id: str) -> bool:
    """Resume follow-up tracking after the system sends an automatic mid-thread reply."""
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return False

        thread_data = thread_doc.to_dict() or {}
        if thread_data.get("status") in {"completed", "stopped"}:
            return False

        followup_config = thread_data.get("followUpConfig", {})
        if not followup_config.get("enabled", False):
            return False

        followups = followup_config.get("followUps", [])
        current_index = followup_config.get("currentFollowUpIndex", 0)
        if current_index >= len(followups):
            return False

        next_followup = followups[current_index]
        wait_time = next_followup.get("waitTime", 3)
        wait_unit = next_followup.get("waitUnit", "days")

        if wait_unit == "minutes":
            delta = timedelta(minutes=wait_time)
        elif wait_unit == "hours":
            delta = timedelta(hours=wait_time)
        else:
            delta = timedelta(days=wait_time)

        next_followup_at = datetime.now(timezone.utc) + delta
        thread_ref.update({
            "followUpStatus": "waiting",
            "followUpConfig.nextFollowUpAt": next_followup_at,
            "followUpConfig.pausedAt": None,
            "hasInboundReply": False,
            "lastOutboundAt": SERVER_TIMESTAMP,
            "updatedAt": SERVER_TIMESTAMP,
        })

        print(f"   Follow-up rescheduled after auto-response for thread {thread_id[:20]}...")
        return True

    except Exception as e:
        print(f"   Error rescheduling follow-up after auto-response: {e}")
        return False


def _pause_followup(user_id: str, thread_id: str):
    """Pause follow-up sequence when broker responds."""
    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpStatus": "paused",
        "followUpConfig.pausedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP
    })
    print(f"   Paused follow-up for thread {thread_id[:20]}... (broker responded)")


def _mark_followup_complete(user_id: str, thread_id: str, reason: str):
    """Mark follow-up sequence as complete."""
    update_data = {
        "followUpStatus": reason,
        "followUpConfig.processingBy": None,
        "followUpConfig.processingAt": None,
        "updatedAt": SERVER_TIMESTAMP
    }
    if reason == "max_reached":
        update_data.update({
            "status": "stopped",
            "statusReason": "max_followups_reached",
        })
        _clear_followup_row_highlight(user_id, thread_id)

    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update(update_data)
    print(f"   Follow-up sequence complete for thread {thread_id[:20]}... ({reason})")


def schedule_followup_for_thread(
    user_id: str,
    thread_id: str,
    followup_config: Dict
):
    """
    Schedule follow-ups for a newly sent thread.
    Called from email.py after sending initial outbound email.

    Args:
        user_id: Firebase user ID
        thread_id: Thread document ID
        followup_config: Configuration from outbox containing:
            - enabled: bool
            - followUps: [{waitTime, waitUnit, message}, ...]
    """
    if not followup_config or not followup_config.get("enabled", False):
        return

    followups = followup_config.get("followUps", [])
    if not followups:
        return

    # Calculate first follow-up time
    first_followup = followups[0]
    wait_time = first_followup.get("waitTime", 5)
    wait_unit = first_followup.get("waitUnit", "days")

    if wait_unit == "minutes":
        delta = timedelta(minutes=wait_time)
    elif wait_unit == "hours":
        delta = timedelta(hours=wait_time)
    else:
        delta = timedelta(days=wait_time)

    next_followup_at = datetime.now(timezone.utc) + delta

    # Update thread with follow-up config
    thread_followup_config = {
        "enabled": True,
        "followUps": followups,
        "currentFollowUpIndex": 0,
        "nextFollowUpAt": next_followup_at,
        "conversationStage": "initial",
        "pausedAt": None,
        "lastFollowUpSentAt": None
    }

    _fs.collection("users").document(user_id).collection("threads").document(thread_id).update({
        "followUpConfig": thread_followup_config,
        "followUpStatus": "waiting",
        "hasInboundReply": False,
        "lastOutboundAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP
    })

    print(f"   Follow-up scheduled: {wait_time} {wait_unit} ({next_followup_at.strftime('%Y-%m-%d %H:%M')} UTC)")


def cancel_followup_on_response(user_id: str, thread_id: str):
    """
    Pause pending follow-up when broker responds.
    Called from processing.py when inbound message is detected.

    The sequence can resume if the broker goes silent again.
    """
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return

        thread_data = thread_doc.to_dict()
        followup_config = thread_data.get("followUpConfig", {})

        if not followup_config.get("enabled", False):
            return

        current_status = thread_data.get("followUpStatus")
        if current_status in ["paused", "completed", "max_reached"]:
            return

        thread_ref.update({
            "hasInboundReply": True,
            "lastInboundAt": SERVER_TIMESTAMP,
            "followUpStatus": "paused",
            "followUpConfig.pausedAt": SERVER_TIMESTAMP,
            "followUpConfig.conversationStage": "mid_conversation",
            "updatedAt": SERVER_TIMESTAMP
        })

        print(f"   Follow-up paused for thread {thread_id[:20]}... (broker responded)")

    except Exception as e:
        print(f"   Error pausing follow-up: {e}")


def resume_followup_if_silent(user_id: str, thread_id: str, silence_threshold_days: int = 3):
    """
    Resume follow-up sequence if broker went silent after responding.

    This is called to check paused threads and see if they should resume.
    Typically called from check_and_send_followups for paused threads.
    """
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_doc = thread_ref.get()

        if not thread_doc.exists:
            return False

        thread_data = thread_doc.to_dict()

        if thread_data.get("followUpStatus") != "paused":
            return False

        last_inbound_at = thread_data.get("lastInboundAt")
        if not last_inbound_at:
            return False

        # Check if enough time has passed since last inbound
        if hasattr(last_inbound_at, 'timestamp'):
            last_inbound_dt = datetime.fromtimestamp(
                last_inbound_at.timestamp(),
                tz=timezone.utc
            )
        else:
            return False

        now = datetime.now(timezone.utc)
        silence_duration = now - last_inbound_dt

        if silence_duration < timedelta(days=silence_threshold_days):
            return False

        # Resume the sequence
        followup_config = thread_data.get("followUpConfig", {})
        current_index = followup_config.get("currentFollowUpIndex", 0)
        followups = followup_config.get("followUps", [])

        if current_index >= len(followups):
            return False

        # Calculate next follow-up time (immediate or short delay)
        next_followup = followups[current_index]
        wait_time = min(next_followup.get("waitTime", 1), 1)  # Cap at 1 day for resumed

        next_followup_at = now + timedelta(days=wait_time)

        thread_ref.update({
            "followUpStatus": "waiting",
            "followUpConfig.nextFollowUpAt": next_followup_at,
            "hasInboundReply": False,  # Reset for next check
            "updatedAt": SERVER_TIMESTAMP
        })

        print(f"   Resumed follow-up for thread {thread_id[:20]}... (broker went silent)")
        return True

    except Exception as e:
        print(f"   Error resuming follow-up: {e}")
        return False


def _get_default_followup_message(index: int) -> str:
    """Return default follow-up message based on sequence position."""
    messages = [
        # Follow-up 1: Friendly reminder
        """Hi [NAME],

I wanted to follow up on my previous email regarding the property above. I understand you're busy, but I wanted to confirm whether this space might be a fit for my client's requirements.

If you could share the key specs (SF, asking rent, NNN, clear height, doors, power), that would be very helpful.

Thanks for your time!""",

        # Follow-up 2: Gentle nudge
        """Hi [NAME],

Just a quick check-in on my earlier emails about the property above. If you have a moment, I'd appreciate any details you can share.

If this property is no longer available or not a good fit, please let me know and I'll update my records.

Thank you!""",

        # Follow-up 3: Final attempt
        """Hi [NAME],

This will be my final follow-up regarding the property above. I'll assume this one isn't a fit for my client's needs, but if you'd like to discuss, I'm happy to connect.

If anything else comes available in the area that might work, please keep me in mind.

Thanks again for your time!"""
    ]

    if index < len(messages):
        return messages[index]
    return messages[-1]
