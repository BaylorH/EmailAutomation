import json
import requests
import time
import uuid
import logging
from typing import Any, Callable, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from google.cloud.firestore import SERVER_TIMESTAMP
from .utils import (
    exponential_backoff_request,
    safe_preview,
    _body_kind,
    validate_recipient_emails,
    is_valid_email,
    resolve_signature_settings,
)
from .messaging import save_thread_root, save_message, index_message_id, index_conversation_id, lookup_thread_by_message_id
from .clients import _get_sheet_id_or_fail, _sheets_client
from .sheets import _find_row_by_email, _get_first_tab_title, _read_header_row2, _header_index_map, highlight_row
from .notifications import delete_notification_and_decrement_counters
from .utils import normalize_message_id

logger = logging.getLogger(__name__)

# Maximum retry attempts before moving to dead-letter queue
MAX_OUTBOX_ATTEMPTS = 5
# Maximum retries for indexing operations
MAX_INDEX_RETRIES = 3
# Claim timeout in seconds (if a claim is older than this, it's considered stale)
CLAIM_TIMEOUT_SECONDS = 300  # 5 minutes

# Outbox items from these dashboard flows already contain operator-reviewed body
# copy. They must not be replaced by contact-history campaign fallback text.
EXACT_OUTBOX_SOURCES = {"dashboard_tour_planner"}
EXACT_OUTBOX_ACTION_TYPES = {"tour_invite"}

# Unique worker ID for this process
WORKER_ID = str(uuid.uuid4())[:8]


def _fresh_graph_headers(
    headers: Dict[str, str],
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
) -> Dict[str, str]:
    if headers_provider:
        fresh_headers = headers_provider()
        if fresh_headers:
            return fresh_headers
    return headers


def _update_action_audit(user_id: str, action_audit_id: Optional[str], payload: Dict[str, Any]) -> None:
    if not action_audit_id:
        return
    try:
        from .clients import _fs
        (
            _fs.collection("users").document(user_id)
            .collection("actionAudit").document(action_audit_id)
            .set(payload, merge=True)
        )
    except Exception as e:
        print(f"   ⚠️ Could not update action audit {action_audit_id}: {e}")


def _terminalize_outbox_action_audit(
    user_id: Optional[str],
    doc_ref,
    data: Dict[str, Any],
    status: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the final dashboard action-audit state for a no-send/send outcome."""
    if not user_id:
        return

    payload = {
        "status": status,
        "outboxId": getattr(doc_ref, "id", None),
        "clientId": data.get("clientId"),
        "notificationId": data.get("notificationId"),
        "threadId": data.get("threadId"),
        "updatedAt": SERVER_TIMESTAMP,
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    _update_action_audit(user_id, data.get("actionAuditId"), payload)


def _first_result_value(mapping: Any, preferred_keys: Optional[List[str]] = None) -> Optional[Any]:
    if not mapping:
        return None
    if isinstance(mapping, dict):
        for key in preferred_keys or []:
            if mapping.get(key):
                return mapping[key]
        for value in mapping.values():
            if value:
                return value
        return None
    if isinstance(mapping, list):
        return next((value for value in mapping if value), None)
    return mapping


def _send_identity_payload(send_result: Optional[Dict[str, Any]], recipients: Optional[List[str]] = None) -> Dict[str, Any]:
    if not send_result:
        return {}

    recipients = recipients or []
    sent_message_ids = send_result.get("sentMessageIds") or {}
    internet_message_ids = send_result.get("internetMessageIds") or {}
    thread_ids = send_result.get("threadIds") or {}
    conversation_ids = send_result.get("conversationIds") or {}

    payload = {
        "sentMessageId": _first_result_value(sent_message_ids, recipients),
        "internetMessageId": _first_result_value(internet_message_ids, recipients),
        "sentThreadId": _first_result_value(thread_ids, recipients),
        "conversationId": _first_result_value(conversation_ids, recipients),
        "sentRecipients": send_result.get("sent"),
        "sentMessageIds": sent_message_ids or None,
        "internetMessageIds": internet_message_ids or None,
        "sentThreadIds": thread_ids or None,
        "conversationIds": conversation_ids or None,
    }
    return {k: v for k, v in payload.items() if v}


def _fetch_graph_message_metadata(headers: dict, message_id: str, base: str) -> Dict[str, Any]:
    if not message_id:
        return {}
    try:
        response = exponential_backoff_request(
            lambda: requests.get(
                f"{base}/me/messages/{message_id}",
                headers=headers,
                params={"$select": "conversationId,subject"},
                timeout=30,
            )
        )
        return response.json() if response else {}
    except Exception as e:
        print(f"   ⚠️ Could not fetch reply source metadata: {e}")
        return {}


def _find_recent_sent_reply_identity(
    headers: dict,
    base: str,
    conversation_id: Optional[str],
    sent_after: datetime,
    *,
    attempts: int = 4,
) -> Dict[str, Any]:
    """Resolve Graph identity for /reply sends, whose API response has no body."""
    if not conversation_id:
        return {}

    sent_after_iso = sent_after.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    params = {
        "$top": "25",
        "$orderby": "sentDateTime desc",
        "$select": "id,internetMessageId,conversationId,subject,sentDateTime",
        "$filter": f"sentDateTime ge {sent_after_iso}",
    }

    for attempt in range(attempts):
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/mailFolders/SentItems/messages",
                    headers=headers,
                    params=params,
                    timeout=30,
                )
            )
            for msg in (response.json() if response else {}).get("value", []):
                if msg.get("conversationId") != conversation_id:
                    continue
                return {
                    "sentMessageId": msg.get("id"),
                    "internetMessageId": msg.get("internetMessageId"),
                    "conversationId": msg.get("conversationId"),
                }
        except Exception as e:
            print(f"   ⚠️ Could not resolve sent reply identity: {e}")

        if attempt < attempts - 1:
            time.sleep(0.75 * (attempt + 1))

    print("   ⚠️ Sent reply identity not found in SentItems yet")
    return {}


def _merge_send_identity(accumulator: Dict[str, Any], send_result: Dict[str, Any]) -> None:
    for key in ("sentMessageIds", "internetMessageIds", "threadIds", "conversationIds"):
        accumulator.setdefault(key, {})
        values = send_result.get(key) or {}
        if isinstance(values, dict):
            accumulator[key].update(values)


def _all_send_errors_are_opt_out(errors: Dict[str, Any]) -> bool:
    if not errors:
        return False
    return all("opted out" in str(message).lower() for message in errors.values())


def _has_existing_thread_for_property(
    user_id: str,
    recipient_email: str,
    property_address: str,
    *,
    client_id: Optional[str] = None,
) -> bool:
    """
    Check if we've already sent an email to this recipient about this property.

    This is a defense-in-depth check to prevent duplicate outreach emails
    even if duplicate outbox entries are somehow created. When a client id is
    known, only threads from that client can block the send; otherwise old test
    or archived campaigns with the same real property address can suppress a
    valid new campaign outreach.

    Returns True if a matching thread already exists, False otherwise.
    """
    from .clients import _fs

    if not recipient_email or not property_address:
        return False

    recipient_lower = recipient_email.lower().strip()

    # Normalize property address for comparison
    # Remove common prefixes and get just the street address
    property_normalized = property_address.lower().strip()
    if ',' in property_normalized:
        property_normalized = property_normalized.split(',')[0].strip()

    try:
        threads_ref = _fs.collection("users").document(user_id).collection("threads")

        # Query threads where this email was a recipient
        query = threads_ref.where("email", "array_contains", recipient_lower)
        results = list(query.stream())

        for thread in results:
            data = thread.to_dict() or {}
            if client_id and data.get("clientId") != client_id:
                continue
            subject = (data.get("subject") or "").lower()

            # Check if subject contains the property address
            if property_normalized in subject:
                print(f"   🔍 Found existing thread for {recipient_email} + '{property_address}'")
                return True

        return False

    except Exception as e:
        print(f"   ⚠️ Error checking for existing thread: {e}")
        # On error, allow send (don't block on lookup failure)
        return False


def _claim_outbox_item(doc_ref, data: dict, user_id: Optional[str] = None) -> bool:
    """
    Attempt to claim an outbox item for processing using a transaction.
    Prevents duplicate sends when multiple processes run concurrently.

    Returns True if successfully claimed, False if already being processed.
    """
    from .clients import _fs
    from google.cloud.firestore import transactional

    cancelled_seen = {}

    @transactional
    def claim_transaction(transaction, doc_ref):
        # Read current state
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            # Item was already deleted
            return False

        current_data = snapshot.to_dict() or {}
        if _is_cancelled_outbox_item(current_data):
            transaction.delete(doc_ref)
            cancelled_seen["data"] = current_data
            print(f"   🗑️ Deleted canceled outbox item {doc_ref.id}")
            return False

        processing_by = current_data.get("processingBy")
        processing_at = current_data.get("processingAt")

        now = datetime.now(timezone.utc)

        # Check if already being processed
        if processing_by and processing_at:
            # Check if claim is stale (older than CLAIM_TIMEOUT_SECONDS)
            if hasattr(processing_at, 'timestamp'):
                # Firestore timestamp
                claim_age = (now - processing_at.replace(tzinfo=timezone.utc)).total_seconds()
            else:
                # Already a datetime
                claim_age = (now - processing_at).total_seconds()

            if claim_age < CLAIM_TIMEOUT_SECONDS:
                # Claim is still valid, skip this item
                print(f"   ⏭️ Item {doc_ref.id} already being processed by {processing_by} ({int(claim_age)}s ago)")
                return False
            else:
                print(f"   ⚠️ Stale claim on {doc_ref.id} by {processing_by} ({int(claim_age)}s ago), reclaiming")

        # Claim the item
        transaction.update(doc_ref, {
            "processingBy": WORKER_ID,
            "processingAt": now
        })
        return True

    try:
        transaction = _fs.transaction()
        claimed = claim_transaction(transaction, doc_ref)
        if not claimed and cancelled_seen:
            _terminalize_outbox_action_audit(
                user_id,
                doc_ref,
                cancelled_seen.get("data") or data,
                "cancelled",
                {"cancelledAt": SERVER_TIMESTAMP},
            )
        return claimed
    except Exception as e:
        print(f"   ⚠️ Failed to claim {doc_ref.id}: {e}")
        return False


def _release_claim(doc_ref):
    """Release claim on an outbox item (called on failure to allow retry)."""
    try:
        doc_ref.update({
            "processingBy": None,
            "processingAt": None
        })
    except Exception as e:
        print(f"   ⚠️ Failed to release claim on {doc_ref.id}: {e}")


def _normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def _get_reply_message_sender(headers: dict, reply_to_msg_id: str) -> Optional[str]:
    """Fetch the sender address of the message a dashboard reply targets."""
    if not reply_to_msg_id:
        return None

    try:
        resp = exponential_backoff_request(
            lambda: requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{reply_to_msg_id}",
                headers=headers,
                params={"$select": "from"},
                timeout=30,
            )
        )
        if not resp or resp.status_code != 200:
            print(f"   ⚠️ Could not resolve reply recipient source: {resp.status_code if resp else 'no response'}")
            return None
        return _normalize_email(
            ((resp.json() or {}).get("from") or {})
            .get("emailAddress", {})
            .get("address", "")
        )
    except Exception as e:
        print(f"   ⚠️ Could not resolve reply recipient source: {e}")
        return None


def _assigned_emails_match_reply_sender(assigned_emails: List[str], reply_sender: Optional[str]) -> bool:
    """True when Graph /reply would send to the same single recipient shown in the UI."""
    normalized = [_normalize_email(email) for email in (assigned_emails or []) if _normalize_email(email)]
    if len(normalized) != 1 or not reply_sender:
        return False
    return normalized[0] == _normalize_email(reply_sender)


def _get_thread_row_number(user_id: str, thread_id: str) -> Optional[int]:
    """Return the stored Sheet row number for a known thread, if present."""
    if not thread_id:
        return None

    try:
        from .clients import _fs
        thread_doc = (
            _fs.collection("users").document(user_id)
            .collection("threads").document(thread_id).get()
        )
        if not thread_doc.exists:
            return None
        row_number = (thread_doc.to_dict() or {}).get("rowNumber")
        return int(row_number) if row_number else None
    except Exception as e:
        print(f"   ⚠️ Could not resolve row number from thread {thread_id[:20]}...: {e}")
        return None


def _save_outbox_reply_message(
    user_id: str,
    thread_id: str,
    assigned_emails: List[str],
    subject: str,
    body: str,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
) -> bool:
    """Persist a dashboard-approved Graph /reply send into the conversation history."""
    if not thread_id:
        return False

    try:
        from .utils import format_email_body_with_footer

        synthetic_id = f"dashboard-reply-{int(time.time() * 1000)}"
        html_body = format_email_body_with_footer(
            body,
            user_signature,
            signature_mode,
            user_email=user_email,
        )
        payload = {
            "direction": "outbound",
            "from": "me",
            "to": assigned_emails or [],
            "subject": subject,
            "body": {
                "content": html_body,
                "preview": safe_preview(body, 300),
            },
            "bodyPreview": safe_preview(body, 300),
            "sentDateTime": datetime.now(timezone.utc).isoformat(),
            "headers": {"internetMessageId": synthetic_id},
            "source": "dashboard_outbox_reply",
        }
        return save_message(user_id, thread_id, synthetic_id, payload)
    except Exception as e:
        print(f"   ⚠️ Could not save dashboard reply message for {thread_id[:20]}...: {e}")
        return False


def _send_outbox_as_reply(user_id: str, headers: dict, body: str, reply_to_msg_id: str,
                          thread_id: str, user_signature: str = None,
                          signature_mode: str = None, user_email: str = None) -> dict:
    """
    Send an outbox item as a reply to an existing message in a thread.

    Used when user responds via frontend to an action_needed notification.
    The email is sent as a reply to maintain thread continuity.

    Returns: dict with 'sent' (bool) and 'error' (str or None)
    """
    from .utils import get_signature_attachments, needs_signature_attachments, format_email_body_with_footer

    base = "https://graph.microsoft.com/v1.0"
    source_metadata = _fetch_graph_message_metadata(headers, reply_to_msg_id, base)

    # Format body as HTML with footer
    html_body = format_email_body_with_footer(
        body,
        user_signature,
        signature_mode,
        user_email=user_email,
    )
    logger.debug(
        "outbox.reply_recipient_resolution",
        extra={
            "user_id": user_id,
            "thread_id": thread_id,
            "reply_to_msg_id": reply_to_msg_id,
            "recipient_source": "microsoft_graph_reply_endpoint",
            "assigned_emails_honored": False,
        },
    )

    try:
        # Check if we need signature attachments (professional mode)
        if needs_signature_attachments(signature_mode, user_signature, user_email=user_email):
            # Use createReply to get a draft, add attachments, then send
            create_reply_resp = exponential_backoff_request(
                lambda: requests.post(f"{base}/me/messages/{reply_to_msg_id}/createReply", headers=headers, timeout=30)
            )

            if create_reply_resp.status_code in [200, 201]:
                reply_draft = create_reply_resp.json()
                reply_draft_id = reply_draft.get("id")

                # Update draft body
                exponential_backoff_request(
                    lambda: requests.patch(
                        f"{base}/me/messages/{reply_draft_id}",
                        headers=headers,
                        json={"body": {"contentType": "HTML", "content": html_body}},
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
                            print(f"   📎 Attached {attachment['name']}")
                    except Exception as e:
                        print(f"   ⚠️ Error attaching {attachment['name']}: {e}")

                # Send the reply
                sent_after = datetime.now(timezone.utc) - timedelta(seconds=10)
                resp = exponential_backoff_request(
                    lambda: requests.post(f"{base}/me/messages/{reply_draft_id}/send", headers=headers, timeout=30)
                )

                if resp and resp.status_code in [200, 202]:
                    print(f"   ✅ Sent reply (with attachments) to thread {thread_id}")
                    identity = _find_recent_sent_reply_identity(
                        headers,
                        base,
                        source_metadata.get("conversationId"),
                        sent_after,
                    )
                    return {"sent": True, "error": None, **identity}
                else:
                    error_msg = f"Send draft failed: {resp.status_code if resp else 'None'}"
                    print(f"   ❌ {error_msg}")
                    return {"sent": False, "error": error_msg}
            else:
                print(f"   ⚠️ createReply failed: {create_reply_resp.status_code}, trying simple reply")

        # Simple reply without attachments
        reply_payload = {
            "message": {
                "body": {
                    "contentType": "HTML",
                    "content": html_body
                }
            }
        }
        sent_after = datetime.now(timezone.utc) - timedelta(seconds=10)
        resp = exponential_backoff_request(
            lambda: requests.post(f"{base}/me/messages/{reply_to_msg_id}/reply",
                                 headers=headers, json=reply_payload, timeout=30)
        )

        if resp and resp.status_code in [200, 201, 202]:
            print(f"   ✅ Sent reply to thread {thread_id}")
            identity = _find_recent_sent_reply_identity(
                headers,
                base,
                source_metadata.get("conversationId"),
                sent_after,
            )
            return {"sent": True, "error": None, **identity}
        else:
            error_msg = f"Reply failed: {resp.status_code if resp else 'None'}"
            print(f"   ❌ {error_msg}")
            return {"sent": False, "error": error_msg}

    except Exception as e:
        error_msg = str(e)
        print(f"   ❌ Error sending reply: {error_msg}")
        return {"sent": False, "error": error_msg}


def get_contact_email_count(user_id: str, recipient_email: str) -> int:
    """
    Count how many outbound emails have been sent to this contact.
    Used to determine whether to use primary or secondary script.
    """
    from .clients import _fs

    threads_ref = _fs.collection("users").document(user_id).collection("threads")

    # Query threads where this email was a recipient
    # The 'email' field is an array of recipient emails
    query = threads_ref.where("email", "array_contains", recipient_email.lower().strip())
    results = list(query.stream())

    return len(results)


def _extract_requirements_from_primary(primary_script: str) -> str:
    """
    Extract the requirements section from a primary script for reuse in fallback scenarios.
    Returns just the requirements bullets/section, not the full script.
    """
    if not primary_script:
        return ""

    # Look for common requirement markers
    markers = ["requirements are:", "requirements:", "looking for:", "they need:", "their requirements"]

    script_lower = primary_script.lower()
    for marker in markers:
        if marker in script_lower:
            idx = script_lower.index(marker)
            # Extract from marker to end of bullet list or paragraph
            requirements_section = primary_script[idx:]

            # Find end (next paragraph break or common closing phrases)
            end_markers = ["if you think", "if it is no longer", "please let me know",
                          "alternatively", "if this might", "thanks"]
            for end in end_markers:
                if end in requirements_section.lower():
                    end_idx = requirements_section.lower().index(end)
                    return requirements_section[:end_idx].strip()

            # No end marker found, return the section (up to reasonable length)
            lines = requirements_section.split('\n')
            result_lines = []
            for line in lines:
                if line.strip() == "":
                    # Empty line might signal end of requirements
                    if len(result_lines) > 0:
                        break
                result_lines.append(line)
            return '\n'.join(result_lines).strip()

    # No requirement marker found, return empty
    return ""


def _select_script_for_recipient(user_id: str, recipient_email: str,
                                  scripts: List[str], contact_name: str = None) -> str:
    """
    Select appropriate script based on contact history.

    scripts is an array where:
    - scripts[0] = Primary script (1st contact)
    - scripts[1] = Secondary script (2nd contact)
    - scripts[2] = 3rd contact script
    - etc.

    If no script exists for the contact count, uses the last available script
    with a "staying organized" note for 3rd+ contacts.
    """
    if not scripts or len(scripts) == 0:
        return ""

    email_count = get_contact_email_count(user_id, recipient_email)
    print(f"📊 Contact history for {recipient_email}: {email_count} previous email(s)")

    # Primary script for first contact
    primary_script = scripts[0]

    if email_count == 0:
        print(f"  → Using script[0] - PRIMARY (first contact)")
        return primary_script

    # For subsequent contacts, try to use the matching script index
    script_index = email_count  # 1st contact uses [0], 2nd uses [1], etc.

    if script_index < len(scripts) and scripts[script_index] and scripts[script_index].strip():
        print(f"  → Using script[{script_index}] ({script_index + 1}{'st' if script_index == 0 else 'nd' if script_index == 1 else 'rd' if script_index == 2 else 'th'} contact)")
        script_to_use = scripts[script_index]

        # Add organized note for 3rd+ contacts
        if email_count >= 2:
            organized_note = "\n\nI want to keep things organized for both of us, so I'm sending separate emails for each of your properties I'm inquiring about."
            return script_to_use.rstrip() + organized_note

        return script_to_use

    # Fallback: use the last available script
    last_script = None
    for s in reversed(scripts):
        if s and s.strip():
            last_script = s
            break

    if last_script and last_script != primary_script:
        print(f"  → Using last available script (fallback for contact #{email_count + 1})")
        if email_count >= 2:
            organized_note = "\n\nI want to keep things organized for both of us, so I'm sending separate emails for each of your properties I'm inquiring about."
            return last_script.rstrip() + organized_note
        return last_script

    # Ultimate fallback: generate from primary
    print(f"  → Using GENERATED fallback (contact #{email_count + 1})")
    requirements = _extract_requirements_from_primary(primary_script)

    # Extract first name from contact_name for greeting
    first_name = None
    if contact_name:
        first_name = contact_name.split()[0] if contact_name.strip() else None
    greeting = f"Hi {first_name}," if first_name else "Hi,"

    if email_count == 1:
        if requirements:
            return f"""{greeting}

I just emailed you about another one of your listings, but I was wondering if you think there might be a fit at the above address as well.

As a reminder, {requirements}

Thanks!"""
        else:
            return f"""{greeting}

I just emailed you about another one of your listings, but I was wondering if you think there might be a fit at the above address as well.

Please let me know if you have any information on this property, or if it's no longer available.

Thanks!"""
    else:
        organized_note = "\n\nI want to keep things organized for both of us, so I'm sending separate emails for each of your properties I'm inquiring about."
        if requirements:
            return f"""{greeting}

I've reached out about a couple of your other listings. I'm also interested in the property at the above address.

As a reminder, {requirements}
{organized_note}

Thanks!"""
        else:
            return f"""{greeting}

I've reached out about a couple of your other listings. I'm also interested in the property at the above address.

Please let me know if you have any information on this property, or if it's no longer available.
{organized_note}

Thanks!"""


def _should_use_exact_outbox_script(data: Dict[str, Any]) -> bool:
    """True when the outbox item contains approved copy that must not be re-selected."""
    source = str(data.get("source") or "").strip().lower()
    action_type = str(data.get("actionType") or "").strip().lower()
    return (
        data.get("scriptSelectionMode") == "exact"
        or data.get("forceScript") is True
        or source in EXACT_OUTBOX_SOURCES
        or action_type in EXACT_OUTBOX_ACTION_TYPES
    )


def _is_cancelled_outbox_item(data: Dict[str, Any]) -> bool:
    """True when the dashboard has requested cancellation before the worker sends."""
    status = (data.get("status") or "").strip().lower()
    return data.get("cancelRequested") is True or status in {"cancel_requested", "cancelled", "canceled"}


def _delete_cancelled_outbox_item_if_needed(
    doc_ref,
    data: Dict[str, Any],
    user_id: Optional[str] = None,
) -> bool:
    if not _is_cancelled_outbox_item(data):
        return False
    doc_id = getattr(doc_ref, "id", "unknown")
    try:
        doc_ref.delete()
        _terminalize_outbox_action_audit(
            user_id,
            doc_ref,
            data,
            "cancelled",
            {"cancelledAt": SERVER_TIMESTAMP},
        )
        print(f"   🗑️ Deleted canceled outbox item {doc_id}")
    except Exception as e:
        print(f"   ⚠️ Could not delete canceled outbox item {doc_id}: {e}")
    return True


def _must_process_outbox_item_individually(data: Dict[str, Any]) -> bool:
    """Dashboard-approved replies/exact-copy items must not be bundled into campaign outreach."""
    return bool(
        data.get("threadId")
        or data.get("replyToMessageId")
        or data.get("notificationId")
        or _should_use_exact_outbox_script(data)
    )


def _get_current_outbox_data(doc_ref) -> Optional[Dict[str, Any]]:
    if not hasattr(doc_ref, "get"):
        return {}
    try:
        snapshot = doc_ref.get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}
    except Exception as e:
        print(f"   ⚠️ Could not refresh outbox item {getattr(doc_ref, 'id', 'unknown')}: {e}")
        return None


def _finalize_successful_outbox_item(
    user_id: str,
    doc_ref,
    data: Dict[str, Any],
    row_number: Optional[int] = None,
    client_id: Optional[str] = None,
    send_result: Optional[Dict[str, Any]] = None,
):
    """Delete sent outbox and apply post-send dashboard state only after send success."""
    from .clients import _fs

    client_id = client_id or (data.get("clientId") or "").strip()

    doc_ref.delete()

    audit_payload = {
        "status": "sent",
        "outboxId": getattr(doc_ref, "id", None),
        "clientId": client_id or None,
        "notificationId": data.get("notificationId"),
        "threadId": data.get("threadId"),
        "sentAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    }
    audit_payload.update(_send_identity_payload(send_result, data.get("assignedEmails") or []))
    _update_action_audit(user_id, data.get("actionAuditId"), audit_payload)

    if row_number and client_id:
        try:
            sheet_id = _get_sheet_id_or_fail(user_id, client_id)
            highlight_row(sheet_id, row_number)
        except Exception as e:
            print(f"  ⚠️ Could not highlight row {row_number}: {e}")

    notification_id = data.get("notificationId")
    notification_client_id = data.get("notificationClientId") or client_id
    if data.get("deleteNotificationOnSend") and notification_id and notification_client_id:
        try:
            delete_notification_and_decrement_counters(user_id, notification_client_id, notification_id)
            print(f"   🗑️ Deleted action notification {notification_id} after send")
        except Exception as e:
            print(f"   ⚠️ Could not delete action notification {notification_id}: {e}")

    thread_id = data.get("threadId")
    if data.get("resumeThreadOnSend") and thread_id:
        try:
            (
                _fs.collection("users").document(user_id)
                .collection("threads").document(thread_id)
                .set({
                    "status": "active",
                    "followUpStatus": "waiting",
                    "lastOperatorReplySentAt": SERVER_TIMESTAMP,
                    "updatedAt": SERVER_TIMESTAMP,
                }, merge=True)
            )
            print(f"   ▶️ Resumed thread {thread_id[:20]}... after dashboard send")
        except Exception as e:
            print(f"   ⚠️ Could not resume thread {thread_id[:20]}... after send: {e}")


def _subject_for_recipient(uid: str, client_id: str, recipient_email: str) -> Optional[str]:
    """
    Look up the row by email and return 'property address, city' as subject.
    Falls back to None if sheet/row/columns not found.
    """
    try:
        sheet_id = _get_sheet_id_or_fail(uid, client_id)
        sheets   = _sheets_client()
        tab      = _get_first_tab_title(sheets, sheet_id)
        header   = _read_header_row2(sheets, sheet_id, tab)

        rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab, header, recipient_email)
        if rownum is None or not rowvals:
            print(f"⚠️ No row found for {recipient_email} in sheet {sheet_id}")
            return None

        # Build a header index map and support common variants
        idx_map = {(h or "").strip().lower(): i for i, h in enumerate(header, start=1)}  # 1-based

        # Try a few reasonable header name variants
        addr_keys = [
            "property address", "address", "street address", "property", "property_address"
        ]
        city_keys = [
            "city", "town", "municipality"
        ]

        def _get_val(keys: List[str]) -> Optional[str]:
            for k in keys:
                if k in idx_map:
                    i = idx_map[k] - 1  # 0-based for rowvals
                    if 0 <= i < len(rowvals):
                        v = (rowvals[i] or "").strip()
                        if v:
                            return v
            return None

        prop = _get_val(addr_keys)
        city = _get_val(city_keys)

        if prop and city:
            return f"{prop}, {city}"
        if prop:
            return prop
        if city:
            return city

        print(f"ℹ️ Address/city columns not found for {recipient_email}")
        return None

    except Exception as e:
        print(f"⚠️ Subject lookup failed for {recipient_email}: {e}")
        return None

def send_and_index_email(user_id: str, headers: Dict[str, str], script: str, recipients: List[str],
                        client_id_or_none: Optional[str] = None, row_number: int = None, user_signature: str = None,
                        subject_override: str = None, signature_mode: str = None, followup_config: Dict = None,
                        contact_name: str = None, user_email: str = None):
    """
    Send email and immediately index it in Firestore for reply tracking.

    Automatically appends the email footer (signature) to all emails.
    For outbox items: script content comes from frontend LLM, footer is appended here.
    For inbox replies: script content may come from backend LLM or templates, footer is appended here.

    Args:
        user_id: The Firebase user ID
        headers: Auth headers for Graph API
        script: The email body content
        recipients: List of recipient email addresses
        client_id_or_none: Optional client ID for tracking
        row_number: Optional row number for thread anchoring
        user_signature: Optional custom signature from user settings
        subject_override: Optional pre-computed subject (e.g., from property data)
        signature_mode: Signature mode - "none", "custom", or "professional"
        followup_config: Optional follow-up configuration from outbox
        contact_name: Optional contact name for follow-up personalization
        user_email: Sender profile email used to gate Jill's explicit legacy footer

    SAFETY: All recipient emails are validated before sending to prevent sending to malformed addresses.
    SAFETY: Opted-out contacts are filtered out before sending.
    """
    if not recipients:
        return {"sent": [], "errors": {"_all": "No recipients"}}

    # CRITICAL: Check for opted-out contacts before sending
    from .processing import is_contact_opted_out
    opted_out_recipients = []
    active_recipients = []

    for recipient in recipients:
        optout_record = is_contact_opted_out(user_id, recipient)
        if optout_record:
            opted_out_recipients.append({
                "email": recipient,
                "reason": optout_record.get("reason", "unknown"),
                "optedOutAt": str(optout_record.get("optedOutAt", ""))
            })
            print(f"🚫 Skipping opted-out contact: {recipient} (reason: {optout_record.get('reason')})")
        else:
            active_recipients.append(recipient)

    if not active_recipients:
        errors = {"_all": "All recipients have opted out"}
        for optout in opted_out_recipients:
            errors[optout["email"]] = f"Contact opted out ({optout['reason']})"
        return {"sent": [], "errors": errors, "opted_out": opted_out_recipients}

    recipients = active_recipients  # Continue with non-opted-out recipients

    # CRITICAL: Validate all recipient emails before sending
    valid_recipients, invalid_recipients = validate_recipient_emails(recipients)

    if invalid_recipients:
        print(f"⚠️ REJECTED invalid email addresses: {invalid_recipients}")

    if not valid_recipients:
        return {"sent": [], "errors": {"_all": f"No valid recipients. Invalid: {invalid_recipients}"}}

    content_type, content = _body_kind(script)

    # Initialize results with opted_out info
    results = {
        "sent": [],
        "errors": {},
        "opted_out": opted_out_recipients,
        "sentMessageIds": {},
        "internetMessageIds": {},
        "threadIds": {},
        "conversationIds": {},
    }

    # Add opted-out recipients to errors for visibility
    for optout in opted_out_recipients:
        results["errors"][optout["email"]] = f"Contact opted out ({optout['reason']})"

    # Append footer to all emails (signature with logo, contact info, etc.)
    from .utils import get_email_footer, format_email_body_with_footer, get_signature_attachments, needs_signature_attachments

    # Check if we need to attach signature images (for professional mode)
    signature_attachments = []
    if needs_signature_attachments(signature_mode, user_signature, user_email=user_email):
        signature_attachments = get_signature_attachments(user_signature, signature_mode, user_email=user_email)
        print(f"📎 Will attach {len(signature_attachments)} signature image(s)")

    if content_type == "HTML":
        # If already HTML, wrap it properly and append footer
        # Check if content is already wrapped in HTML structure
        footer_html = get_email_footer(user_signature, signature_mode, user_email=user_email)
        if not content.strip().startswith("<!DOCTYPE") and not content.strip().startswith("<html"):
            # Wrap existing HTML content and add footer (only if footer exists)
            if footer_html:
                content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
</head>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000; margin: 0; padding: 0;">
<div style="max-width: 600px;">
{content}
<!-- Email signature separator - prevents collapse -->
<div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;">
<div style="margin-top: 20px;">
{footer_html}
</div>
</div>
</div>
</body>
</html>"""
            else:
                # No footer - just wrap in HTML structure
                content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
</head>
<body style="font-family: Arial, Helvetica, sans-serif; font-size: 10pt; color: #000000; margin: 0; padding: 0;">
<div style="max-width: 600px;">
{content}
</div>
</body>
</html>"""
        else:
            # Content is already wrapped, only append footer if it exists
            if footer_html:
                if "</body>" in content:
                    footer_with_wrapper = f'<!-- Email signature separator - prevents collapse --><div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;"><div style="margin-top: 20px;">{footer_html}</div></div>'
                    content = content.replace("</body>", footer_with_wrapper + "</body>")
                else:
                    # No body tag, just append
                    content = content + f'<!-- Email signature separator - prevents collapse --><div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;"><div style="margin-top: 20px;">{footer_html}</div></div>'
            # If no footer, leave content as-is
    else:
        # Convert to HTML and add footer (this function now wraps in proper HTML structure)
        content = format_email_body_with_footer(
            content,
            user_signature,
            signature_mode,
            user_email=user_email,
        )
        content_type = "HTML"
    base = "https://graph.microsoft.com/v1.0"

    # Add invalid recipients to errors
    for invalid in invalid_recipients:
        results["errors"][invalid] = "Invalid email address format"

    for addr in valid_recipients:
        # Use pre-computed subject if provided, otherwise look up from sheet
        if subject_override:
            subject_to_use = subject_override
        else:
            dynamic_subject = None
            if client_id_or_none:
                dynamic_subject = _subject_for_recipient(user_id, client_id_or_none, (addr or "").lower())
            subject_to_use = dynamic_subject or "Client Outreach"

        msg = {
            "subject": subject_to_use,
            "body": {"contentType": content_type, "content": content},
            "toRecipients": [{"emailAddress": {"address": addr}}],
        }
        
        # Add headers
        internet_headers = []
        if client_id_or_none:
            internet_headers.append({"name": "x-client-id", "value": client_id_or_none})
        if row_number:
            internet_headers.append({"name": "x-row-anchor", "value": f"rowNumber={row_number}"})
        
        if internet_headers:
            msg["internetMessageHeaders"] = internet_headers

        try:
            # 1. Create draft
            create_response = exponential_backoff_request(
                lambda: requests.post(f"{base}/me/messages", headers=headers, json=msg, timeout=30)
            )
            draft_id = create_response.json()["id"]
            print(f"📝 Created draft {draft_id} for {addr}")

            # 1b. Add signature image attachments (for professional mode)
            # CID (Content-ID) attachments are the most reliable way to embed images in emails
            if signature_attachments:
                for attachment in signature_attachments:
                    attach_response = exponential_backoff_request(
                        lambda att=attachment: requests.post(
                            f"{base}/me/messages/{draft_id}/attachments",
                            headers=headers,
                            json=att,
                            timeout=30
                        )
                    )
                    if attach_response.status_code in [200, 201]:
                        print(f"   📎 Attached {attachment['name']}")
                    else:
                        print(f"   ⚠️ Failed to attach {attachment['name']}: {attach_response.status_code}")

            # 2. Get message identifiers
            get_response = exponential_backoff_request(
                lambda: requests.get(
                    f"{base}/me/messages/{draft_id}",
                    headers=headers,
                    params={"$select": "internetMessageId,conversationId,subject,toRecipients"},
                    timeout=30
                )
            )
            message_data = get_response.json()

            internet_message_id = message_data.get("internetMessageId")
            conversation_id = message_data.get("conversationId")
            subject = message_data.get("subject", "")

            if not internet_message_id:
                raise Exception("No internetMessageId returned from Graph")

            # 3. Send draft
            exponential_backoff_request(
                lambda: requests.post(f"{base}/me/messages/{draft_id}/send", headers=headers, timeout=30)
            )

            # 4. Index in Firestore with retry logic
            # CRITICAL: Email is already sent at this point. We MUST index it successfully
            # or future replies will be orphaned (unable to match to this thread).
            root_id = normalize_message_id(internet_message_id)
            results["sentMessageIds"][addr] = draft_id
            results["internetMessageIds"][addr] = internet_message_id
            results["threadIds"][addr] = root_id
            results["conversationIds"][addr] = conversation_id

            # Thread root
            thread_meta = {
                "subject": subject,
                "clientId": client_id_or_none,
                "email": [addr],
                "conversationId": conversation_id,
                "status": "active",  # New threads start as active
            }

            # Store row number for anchoring if provided
            if row_number:
                thread_meta["rowNumber"] = row_number

            # Store contact name for follow-up personalization
            if contact_name:
                thread_meta["contactName"] = contact_name

            # Store property address for PDF/data matching
            # Extract from subject (format: "Property Address, City")
            if subject:
                # Remove common prefixes like "Re:", "RE:", "Fwd:", etc.
                clean_subject = subject.strip()
                for prefix in ["Re:", "RE:", "Fwd:", "FWD:", "Fw:"]:
                    if clean_subject.startswith(prefix):
                        clean_subject = clean_subject[len(prefix):].strip()
                thread_meta["propertyAddress"] = clean_subject

            # Save thread root with retry
            thread_saved = False
            for attempt in range(MAX_INDEX_RETRIES):
                if save_thread_root(user_id, root_id, thread_meta):
                    thread_saved = True
                    break
                print(f"⚠️ Thread save attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                time.sleep(0.5 * (attempt + 1))  # Backoff

            if not thread_saved:
                raise Exception(f"Failed to save thread root after {MAX_INDEX_RETRIES} attempts - replies will be orphaned")

            # Message record
            message_record = {
                "direction": "outbound",
                "subject": subject,
                "from": "me",  # Graph doesn't return our own address easily
                "to": [addr],
                "sentDateTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "receivedDateTime": None,
                "headers": {
                    "internetMessageId": internet_message_id,
                    "inReplyTo": None,
                    "references": []
                },
                "body": {
                    "contentType": content_type,
                    "content": content,
                    "preview": safe_preview(content)
                }
            }

            # Save message with retry
            message_saved = False
            for attempt in range(MAX_INDEX_RETRIES):
                if save_message(user_id, root_id, root_id, message_record):
                    message_saved = True
                    break
                print(f"⚠️ Message save attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                time.sleep(0.5 * (attempt + 1))

            if not message_saved:
                print(f"⚠️ Failed to save message record after {MAX_INDEX_RETRIES} attempts (thread exists, non-critical)")

            # Index message ID with retry and verification (CRITICAL for reply matching)
            msg_indexed = False
            for attempt in range(MAX_INDEX_RETRIES):
                if index_message_id(user_id, internet_message_id, root_id):
                    # Verify the index was actually written
                    time.sleep(0.2)  # Brief delay for consistency
                    if lookup_thread_by_message_id(user_id, internet_message_id) == root_id:
                        msg_indexed = True
                        break
                    print(f"⚠️ Index verification failed on attempt {attempt + 1}")
                print(f"⚠️ Message index attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                time.sleep(0.5 * (attempt + 1))

            if not msg_indexed:
                raise Exception(f"CRITICAL: Failed to index message ID after {MAX_INDEX_RETRIES} attempts - replies will be orphaned")

            # Index conversation ID with retry (fallback lookup, less critical but still important)
            if conversation_id:
                conv_indexed = False
                for attempt in range(MAX_INDEX_RETRIES):
                    if index_conversation_id(user_id, conversation_id, root_id):
                        conv_indexed = True
                        break
                    print(f"⚠️ Conversation index attempt {attempt + 1}/{MAX_INDEX_RETRIES} failed, retrying...")
                    time.sleep(0.5 * (attempt + 1))

                if not conv_indexed:
                    # Log but don't fail - message ID index is the primary lookup
                    print(f"⚠️ Failed to index conversation ID (fallback) - primary index succeeded")

            results["sent"].append(addr)
            print(f"✅ Sent and indexed email to {addr} (threadId: {root_id})")

            # Schedule follow-up if configured
            if followup_config and followup_config.get("enabled", False):
                from .followup import schedule_followup_for_thread
                from .clients import _fs
                # Store contact name on thread for follow-up personalization
                if contact_name:
                    _fs.collection("users").document(user_id).collection("threads").document(root_id).update({
                        "contactName": contact_name
                    })
                schedule_followup_for_thread(user_id, root_id, followup_config)

        except Exception as e:
            msg = str(e)
            print(f"❌ Failed to send/index to {addr}: {msg}")
            results["errors"][addr] = msg

    return results

def _move_to_dead_letter(user_id: str, doc_ref, data: dict, reason: str):
    """Move a failed outbox item to the dead-letter queue for manual review."""
    from .clients import _fs
    from google.cloud.firestore import SERVER_TIMESTAMP

    dead_letter_ref = _fs.collection("users").document(user_id).collection("deadLetterQueue")

    # Copy data to dead-letter queue with failure info
    dead_letter_data = {
        **data,
        "originalDocId": doc_ref.id,
        "failureReason": reason,
        "movedAt": SERVER_TIMESTAMP,
        "source": "outbox"
    }

    dead_letter_ref.add(dead_letter_data)
    _update_action_audit(user_id, data.get("actionAuditId"), {
        "status": "dead_lettered",
        "outboxId": getattr(doc_ref, "id", None),
        "clientId": data.get("clientId"),
        "notificationId": data.get("notificationId"),
        "threadId": data.get("threadId"),
        "failureReason": reason,
        "deadLetteredAt": SERVER_TIMESTAMP,
        "failedAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
    })
    doc_ref.delete()
    print(f"☠️ Moved item {doc_ref.id} to dead-letter queue: {reason}")


def send_outboxes(
    user_id: str,
    headers: Dict[str, str],
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
):
    """
    Process outbox items: read script content (generated by frontend LLM), append footer, and send.

    Flow:
    1. Frontend LLM generates email content and writes to Firestore outbox with 'script' field
    2. Backend reads script as-is (no LLM processing here)
    3. If multiple properties are queued for the same broker, combine into one natural email
    4. Footer is automatically appended by send_and_index_email()
    5. Email is sent and indexed for reply tracking

    Items are retried up to MAX_OUTBOX_ATTEMPTS times, then moved to dead-letter queue.
    """
    from .clients import _fs
    from collections import defaultdict

    # Fetch user's email signature settings
    user_doc = _fs.collection("users").document(user_id).get()
    user_signature = None
    signature_mode = None
    user_email = None
    if user_doc.exists:
        user_data = user_doc.to_dict() or {}
        user_signature, signature_mode, user_email = resolve_signature_settings(user_data)
        if signature_mode:
            print(f"📝 Signature mode: {signature_mode}")
        elif user_signature:
            print(f"📝 Using custom email signature for user")

    outbox_ref = _fs.collection("users").document(user_id).collection("outbox")
    # Order by createdAt to send emails in the order they were queued (oldest first)
    docs = list(outbox_ref.order_by("createdAt").stream())

    if not docs:
        print("📭 Outbox empty")
        return

    print(f"📬 Found {len(docs)} outbox item(s)")

    # Group outbox items by recipient email to detect multi-property scenarios
    email_groups = defaultdict(list)
    for d in docs:
        data = d.to_dict() or {}
        if _delete_cancelled_outbox_item_if_needed(d.reference, data, user_id=user_id):
            continue
        emails = data.get("assignedEmails") or []

        if _must_process_outbox_item_individually(data):
            email_lower = emails[0].lower().strip() if emails else f"__no_recipient__:{d.id}"
            email_groups[f"__single__:{d.id}"].append({
                'doc': d,
                'data': data,
                'email': email_lower
            })
            continue

        for email in emails:
            email_lower = email.lower().strip()
            email_groups[email_lower].append({
                'doc': d,
                'data': data,
                'email': email
            })

    # Process each unique recipient
    recipients_list = list(email_groups.items())
    for idx, (recipient_email, items) in enumerate(recipients_list):
        # Filter out items that have exceeded max attempts
        valid_items = []
        for item in items:
            data = item['data']
            attempts = int(data.get("attempts") or 0)
            if attempts >= MAX_OUTBOX_ATTEMPTS:
                _move_to_dead_letter(
                    user_id, item['doc'].reference, data,
                    f"Exceeded max attempts ({MAX_OUTBOX_ATTEMPTS}): {data.get('lastError', 'unknown error')}"
                )
            else:
                valid_items.append(item)

        if not valid_items:
            continue

        # Check if multiple properties for same broker
        if len(valid_items) > 1:
            print(f"🔗 Detected {len(valid_items)} properties for same broker: {recipient_email}")
            _send_multi_property_email(
                user_id,
                _fresh_graph_headers(headers, headers_provider),
                recipient_email,
                valid_items,
                user_signature,
                signature_mode,
                user_email,
                headers_provider=headers_provider,
            )
        else:
            # Single property - send normally
            item = valid_items[0]
            _send_single_outbox_item(
                user_id,
                _fresh_graph_headers(headers, headers_provider),
                item,
                user_signature,
                signature_mode,
                user_email,
                headers_provider=headers_provider,
            )

        # 2-minute delay between ALL emails to avoid spam detection
        if idx < len(recipients_list) - 1:
            print(f"  ⏳ Waiting 2 minutes before next recipient to avoid spam detection...")
            time.sleep(120)


def _send_multi_property_email(
    user_id: str,
    headers: Dict[str, str],
    recipient_email: str,
    items: list,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
):
    """
    Send SEPARATE emails for multiple properties to the same broker.
    Each property gets its own thread for clean tracking.
    The first email acknowledges there are multiple and explains the organization strategy.
    """
    # Check for opted-out contacts first
    from .processing import is_contact_opted_out
    optout_record = is_contact_opted_out(user_id, recipient_email)
    if optout_record:
        print(f"🚫 Skipping multi-property emails to opted-out contact: {recipient_email}")
        # Delete all outbox items for this recipient
        for item in items:
            _terminalize_outbox_action_audit(
                user_id,
                item['doc'].reference,
                item['data'],
                "opt_out_skipped",
                {
                    "skippedAt": SERVER_TIMESTAMP,
                    "skipReason": optout_record.get("reason", "contact_opted_out"),
                },
            )
            item['doc'].reference.delete()
        print(f"🗑️ Deleted {len(items)} outbox items (recipient opted out)")
        return

    # Extract property info from each item
    properties = []
    for item in items:
        data = item['data']
        if _delete_cancelled_outbox_item_if_needed(item['doc'].reference, data, user_id=user_id):
            continue
        subject = data.get("subject", "")
        # Try to extract property address from subject or script
        property_name = subject or _extract_property_from_script(data.get("script", ""))
        properties.append({
            'item': item,
            'name': property_name,
            'subject': subject,  # Pre-computed subject from property data
            'clientId': data.get("clientId", ""),
            'script': data.get("script", ""),
            'rowNumber': data.get("rowNumber")
        })

    print(f"📬 Sending {len(properties)} separate property emails to {recipient_email}")

    # Send each property as its own email/thread
    # Each email uses the exact script that was approved in the frontend
    for idx, prop in enumerate(properties):
        item = prop['item']
        data = item['data']

        if _delete_cancelled_outbox_item_if_needed(item['doc'].reference, data, user_id=user_id):
            continue

        # CRITICAL: Claim the item before processing to prevent duplicate sends
        if not _claim_outbox_item(item['doc'].reference, data, user_id=user_id):
            print(f"   ⏭️ Skipping property {idx + 1} - already being processed by another worker")
            continue

        fresh_data = _get_current_outbox_data(item['doc'].reference)
        if fresh_data is None:
            continue
        if fresh_data:
            data = fresh_data

        if _delete_cancelled_outbox_item_if_needed(item['doc'].reference, data, user_id=user_id):
            continue

        clientId = (data.get("clientId") or "").strip()
        attempts = int(data.get("attempts") or 0)
        row_number = data.get("rowNumber") or prop.get('rowNumber')

        # DUPLICATE CHECK: Skip if we've already sent to this recipient about this property
        property_address = prop.get('subject') or prop.get('name') or ''
        if _has_existing_thread_for_property(user_id, recipient_email, property_address, client_id=clientId):
            print(f"   🚫 DUPLICATE DETECTED: Already sent to {recipient_email} about '{property_address}'")
            print(f"   🗑️ Deleting duplicate outbox entry")
            _terminalize_outbox_action_audit(
                user_id,
                item['doc'].reference,
                data,
                "duplicate_skipped",
                {"skippedAt": SERVER_TIMESTAMP, "skipReason": "existing_thread_for_property"},
            )
            item['doc'].reference.delete()
            continue

        # Use the original approved script without modification
        script = data.get("script", prop['script'])

        print(f"  → Property {idx + 1}/{len(properties)}: {prop['name'] or 'Unknown'} (attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")

        try:
            # Use pre-computed subject from property data
            subject_override = prop.get('subject') or None
            followup_config = data.get("followUpConfig")

            # Fallback: fetch followUpConfig from client if not on outbox item
            if not followup_config and clientId:
                try:
                    from .clients import _fs
                    client_doc = _fs.collection("users").document(user_id).collection("clients").document(clientId).get()
                    if client_doc.exists:
                        client_data = client_doc.to_dict()
                        followup_config = client_data.get("followUpConfig")
                        if followup_config:
                            print(f"   📋 Fetched followUpConfig from client (enabled={followup_config.get('enabled')})")
                except Exception as e:
                    print(f"   ⚠️ Could not fetch followUpConfig from client: {e}")

            contact_name = data.get("contactName") or data.get("firstName")
            current_headers = _fresh_graph_headers(headers, headers_provider)
            res = send_and_index_email(user_id, current_headers, script, [recipient_email],
                                       client_id_or_none=clientId, row_number=row_number,
                                       user_signature=user_signature, subject_override=subject_override,
                                       signature_mode=signature_mode, followup_config=followup_config,
                                       contact_name=contact_name, user_email=user_email)
            any_errors = bool([e for e in res.get("errors", {}) if "opted out" not in str(res["errors"].get(e, ""))])

            if not any_errors and res["sent"]:
                _finalize_successful_outbox_item(
                    user_id, item['doc'].reference, data,
                    row_number=row_number, client_id=clientId,
                    send_result=res,
                )
                print(f"  ✅ Sent and deleted outbox item for {prop['name']}")
            elif not res.get("sent") and res.get("opted_out") and _all_send_errors_are_opt_out(res.get("errors", {})):
                _terminalize_outbox_action_audit(
                    user_id,
                    item['doc'].reference,
                    data,
                    "opt_out_skipped",
                    {"skippedAt": SERVER_TIMESTAMP, "skipReason": "contact_opted_out"},
                )
                item['doc'].reference.delete()
                print(f"  🚫 Deleted outbox item for opted-out recipient {recipient_email}")
            else:
                new_attempts = attempts + 1
                error_msg = json.dumps(res["errors"])[:1500]

                if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                    _move_to_dead_letter(user_id, item['doc'].reference, data,
                        f"Send errors after {new_attempts} attempts: {error_msg}")
                else:
                    # Release claim and update attempts so it can be retried
                    item['doc'].reference.set(
                        {"attempts": new_attempts, "lastError": error_msg, "processingBy": None, "processingAt": None},
                        merge=True,
                    )
                print(f"  ⚠️ Kept item with error; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")

        except Exception as e:
            new_attempts = attempts + 1
            error_msg = str(e)[:1500]

            if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                _move_to_dead_letter(user_id, item['doc'].reference, data,
                    f"Exception after {new_attempts} attempts: {error_msg}")
            else:
                # Release claim and update attempts so it can be retried
                item['doc'].reference.set(
                    {"attempts": new_attempts, "lastError": error_msg, "processingBy": None, "processingAt": None},
                    merge=True,
                )
            print(f"  💥 Error: {e}; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")

        # 2-minute delay between emails to same recipient to avoid spam flags
        if idx < len(properties) - 1:
            print(f"  ⏳ Waiting 2 minutes before sending next email to avoid spam detection...")
            time.sleep(120)


def _send_single_outbox_item(
    user_id: str,
    headers: Dict[str, str],
    item: dict,
    user_signature: str = None,
    signature_mode: str = None,
    user_email: str = None,
    headers_provider: Optional[Callable[[], Dict[str, str]]] = None,
):
    """
    Send a single outbox item with smart script selection based on contact history.

    The script selection logic:
    - scripts[0] = 1st contact (primary)
    - scripts[1] = 2nd contact (follow-up)
    - scripts[2] = 3rd contact, etc.

    Uses claim mechanism to prevent duplicate sends when multiple processes run concurrently.
    """
    d = item['doc']
    data = item['data']

    if _delete_cancelled_outbox_item_if_needed(d.reference, data, user_id=user_id):
        return

    # CRITICAL: Claim the item before processing to prevent duplicate sends
    if not _claim_outbox_item(d.reference, data, user_id=user_id):
        print(f"   ⏭️ Skipping {d.id} - already being processed by another worker")
        return

    fresh_data = _get_current_outbox_data(d.reference)
    if fresh_data is None:
        return
    if fresh_data:
        data = fresh_data

    if _delete_cancelled_outbox_item_if_needed(d.reference, data, user_id=user_id):
        return

    emails = data.get("assignedEmails") or []
    clientId = (data.get("clientId") or "").strip()
    attempts = int(data.get("attempts") or 0)
    row_number = data.get("rowNumber")
    thread_id = data.get("threadId")
    reply_to_msg_id = data.get("replyToMessageId")

    # If row_number is missing (e.g., user reply from UI), prefer the known thread anchor.
    # Broker email can appear on several rows in a campaign; email fallback is only safe for new outreach.
    if not row_number and thread_id:
        row_number = _get_thread_row_number(user_id, thread_id)
        if row_number:
            print(f"   📍 Resolved row number from thread {thread_id[:20]}...: {row_number}")

    if not row_number and emails and clientId:
        try:
            sheet_id = _get_sheet_id_or_fail(user_id, clientId)
            sheets = _sheets_client()
            tab_title = _get_first_tab_title(sheets, sheet_id)
            sheet_headers = _read_header_row2(sheets, sheet_id, tab_title)
            row_number, _row_values = _find_row_by_email(sheets, sheet_id, tab_title, sheet_headers, emails[0])
            if row_number:
                print(f"   📍 Looked up row number: {row_number} for {emails[0]}")
        except Exception as e:
            print(f"   ⚠️ Could not look up row number: {e}")

    # Get pre-computed subject from outbox data (property-specific)
    subject_override = data.get("subject")

    # Threading support: check if this is a reply to an existing thread
    is_thread_reply = bool(thread_id and reply_to_msg_id)

    # Get scripts array (new format) or build from legacy fields
    email_scripts = data.get("emailScripts")
    if not email_scripts or len(email_scripts) == 0:
        # Fallback to legacy script/secondaryScript fields
        primary_script = data.get("script") or ""
        secondary_script = data.get("secondaryScript")
        email_scripts = [primary_script]
        if secondary_script:
            email_scripts.append(secondary_script)

    if is_thread_reply:
        print(f"→ Sending outbox item {d.id} as REPLY to thread {thread_id} (attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")
    else:
        print(f"→ Sending outbox item {d.id} to {len(emails)} recipient(s) (clientId={clientId or 'n/a'}, {len(email_scripts)} script(s), attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")

        # DUPLICATE CHECK: For new outreach (not replies), check if thread already exists
        # Only check if we have a subject (property address)
        if subject_override and len(emails) == 1:
            if _has_existing_thread_for_property(user_id, emails[0], subject_override, client_id=clientId):
                print(f"   🚫 DUPLICATE DETECTED: Already sent to {emails[0]} about '{subject_override}'")
                print(f"   🗑️ Deleting duplicate outbox entry")
                _terminalize_outbox_action_audit(
                    user_id,
                    d.reference,
                    data,
                    "duplicate_skipped",
                    {"skippedAt": SERVER_TIMESTAMP, "skipReason": "existing_thread_for_property"},
                )
                d.reference.delete()
                return

    # Track results for all recipients
    all_sent = []
    all_errors = {}
    send_identity = {
        "sentMessageIds": {},
        "internetMessageIds": {},
        "threadIds": {},
        "conversationIds": {},
    }

    # Get follow-up config if present
    followup_config = data.get("followUpConfig")

    # Fallback: fetch followUpConfig from client if not on outbox item
    if not followup_config and clientId:
        try:
            from .clients import _fs
            client_doc = _fs.collection("users").document(user_id).collection("clients").document(clientId).get()
            if client_doc.exists:
                client_data = client_doc.to_dict()
                followup_config = client_data.get("followUpConfig")
                if followup_config:
                    print(f"   📋 Fetched followUpConfig from client (enabled={followup_config.get('enabled')})")
        except Exception as e:
            print(f"   ⚠️ Could not fetch followUpConfig from client: {e}")

    contact_name = data.get("contactName") or data.get("firstName")

    # If this is a reply to an existing thread, use _send_outbox_as_reply
    if is_thread_reply:
        # For replies, use the script directly (already personalized by frontend)
        script_content = email_scripts[0] if email_scripts else ""
        current_headers = _fresh_graph_headers(headers, headers_provider)
        reply_sender = _get_reply_message_sender(current_headers, reply_to_msg_id)
        use_graph_reply = _assigned_emails_match_reply_sender(emails, reply_sender)

        if use_graph_reply:
            try:
                current_headers = _fresh_graph_headers(headers, headers_provider)
                res = _send_outbox_as_reply(
                    user_id, current_headers, script_content, reply_to_msg_id,
                    thread_id, user_signature=user_signature,
                    signature_mode=signature_mode, user_email=user_email
                )

                if res.get("sent"):
                    recipient = emails[0] if emails else "unknown"
                    all_sent.append(recipient)
                    if res.get("sentMessageId"):
                        send_identity["sentMessageIds"][recipient] = res.get("sentMessageId")
                    if res.get("internetMessageId"):
                        send_identity["internetMessageIds"][recipient] = res.get("internetMessageId")
                    if res.get("conversationId"):
                        send_identity["conversationIds"][recipient] = res.get("conversationId")
                    _save_outbox_reply_message(
                        user_id, thread_id, emails, subject_override,
                        script_content, user_signature, signature_mode, user_email
                    )
                else:
                    all_errors[emails[0] if emails else "unknown"] = res.get("error", "Unknown error")

            except Exception as e:
                all_errors[emails[0] if emails else "unknown"] = str(e)
                print(f"💥 Error sending reply: {e}")
        else:
            if emails and reply_sender:
                print(f"   ↪️ Dashboard recipient differs from reply sender ({reply_sender}); sending new indexed message")
            elif emails:
                print("   ↪️ Could not verify Graph reply recipient; sending new indexed message to dashboard recipient")
            else:
                all_errors["_all"] = "Thread reply has no assigned recipient"

            for recipient_email in emails:
                try:
                    current_headers = _fresh_graph_headers(headers, headers_provider)
                    res = send_and_index_email(
                        user_id, current_headers, script_content, [recipient_email],
                        client_id_or_none=clientId, row_number=row_number,
                        user_signature=user_signature, subject_override=subject_override,
                        signature_mode=signature_mode, followup_config=followup_config,
                        contact_name=contact_name, user_email=user_email
                    )
                    all_sent.extend(res.get("sent", []))
                    all_errors.update(res.get("errors", {}))
                    _merge_send_identity(send_identity, res)
                except Exception as e:
                    all_errors[recipient_email] = str(e)
                    print(f"💥 Error sending redirected thread reply to {recipient_email}: {e}")
    else:
        # For each recipient, select the appropriate script based on contact history
        use_exact_script = _should_use_exact_outbox_script(data)
        for recipient_email in emails:
            if use_exact_script:
                selected_script = email_scripts[0] if email_scripts else ""
                print(f"  → Using exact outbox script for {recipient_email}")
            else:
                selected_script = _select_script_for_recipient(
                    user_id, recipient_email, email_scripts, contact_name=contact_name
                )

            try:
                current_headers = _fresh_graph_headers(headers, headers_provider)
                res = send_and_index_email(user_id, current_headers, selected_script, [recipient_email],
                                           client_id_or_none=clientId, row_number=row_number,
                                           user_signature=user_signature, subject_override=subject_override,
                                           signature_mode=signature_mode, followup_config=followup_config,
                                           contact_name=contact_name, user_email=user_email)

                all_sent.extend(res.get("sent", []))
                all_errors.update(res.get("errors", {}))
                _merge_send_identity(send_identity, res)

            except Exception as e:
                all_errors[recipient_email] = str(e)
                print(f"💥 Error sending to {recipient_email}: {e}")

    # Determine success/failure for the outbox item
    any_errors = bool(all_errors)

    if not any_errors and all_sent:
        _finalize_successful_outbox_item(
            user_id, d.reference, data,
            row_number=row_number, client_id=clientId,
            send_result={**send_identity, "sent": all_sent},
        )
        print(f"🗑️ Deleted outbox item {d.id}")
    elif not all_sent and _all_send_errors_are_opt_out(all_errors):
        _terminalize_outbox_action_audit(
            user_id,
            d.reference,
            data,
            "opt_out_skipped",
            {"skippedAt": SERVER_TIMESTAMP, "skipReason": "contact_opted_out"},
        )
        d.reference.delete()
        print(f"🚫 Deleted outbox item {d.id}; all recipients opted out")
    else:
        new_attempts = attempts + 1
        error_msg = json.dumps(all_errors)[:1500]

        if new_attempts >= MAX_OUTBOX_ATTEMPTS:
            _move_to_dead_letter(user_id, d.reference, data, f"Send errors after {new_attempts} attempts: {error_msg}")
        else:
            # Release claim and update attempts so it can be retried
            d.reference.set(
                {"attempts": new_attempts, "lastError": error_msg, "processingBy": None, "processingAt": None},
                merge=True,
            )
            print(f"⚠️ Kept item {d.id} with error; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")


def _extract_property_from_script(script: str) -> str:
    """Try to extract property address from email script."""
    import re
    # Look for common patterns like "123 Main St" or "at 456 Oak Ave"
    patterns = [
        r'(?:about|for|at|regarding)\s+(\d+[^,.\n]+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Way|Lane|Ln|Ct|Court|Pl|Place)[^\n,]*)',
        r'(\d+\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Dr|Drive|Rd|Road|Way|Lane|Ln|Ct|Court|Pl|Place))',
    ]
    for pattern in patterns:
        match = re.search(pattern, script, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""

# Legacy Functions (kept for compatibility)
def send_email(headers, script: str, emails: List[str], client_id: Optional[str] = None):
    """Legacy function - redirects to send_and_index_email"""
    # Note: This legacy function doesn't have user_id, so it can't use the new pipeline
    # Users should migrate to send_and_index_email directly
    raise NotImplementedError("send_email is deprecated. Use send_and_index_email with user_id parameter.")
