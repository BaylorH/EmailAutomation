import json
import requests
import time
from typing import Dict, List, Optional
from datetime import datetime, timezone
from .utils import exponential_backoff_request, safe_preview, _body_kind, validate_recipient_emails, is_valid_email
from .messaging import save_thread_root, save_message, index_message_id, index_conversation_id, lookup_thread_by_message_id
from .clients import _get_sheet_id_or_fail, _sheets_client
from .sheets import _find_row_by_email, _get_first_tab_title, _read_header_row2, _header_index_map
from .utils import normalize_message_id

# Maximum retry attempts before moving to dead-letter queue
MAX_OUTBOX_ATTEMPTS = 5
# Maximum retries for indexing operations
MAX_INDEX_RETRIES = 3

def _subject_for_recipient(uid: str, client_id: str, recipient_email: str) -> str | None:
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

        def _get_val(keys: list[str]) -> str | None:
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
                        client_id_or_none: Optional[str] = None, row_number: int = None):
    """
    Send email and immediately index it in Firestore for reply tracking.

    Automatically appends the email footer (signature) to all emails.
    For outbox items: script content comes from frontend LLM, footer is appended here.
    For inbox replies: script content may come from backend LLM or templates, footer is appended here.

    SAFETY: All recipient emails are validated before sending to prevent sending to malformed addresses.
    """
    if not recipients:
        return {"sent": [], "errors": {"_all": "No recipients"}}

    # CRITICAL: Validate all recipient emails before sending
    valid_recipients, invalid_recipients = validate_recipient_emails(recipients)

    if invalid_recipients:
        print(f"⚠️ REJECTED invalid email addresses: {invalid_recipients}")

    if not valid_recipients:
        return {"sent": [], "errors": {"_all": f"No valid recipients. Invalid: {invalid_recipients}"}}

    content_type, content = _body_kind(script)
    
    # Append footer to all emails (signature with logo, contact info, etc.)
    from .utils import get_email_footer, format_email_body_with_footer
    if content_type == "HTML":
        # If already HTML, wrap it properly and append footer
        # Check if content is already wrapped in HTML structure
        if not content.strip().startswith("<!DOCTYPE") and not content.strip().startswith("<html"):
            # Wrap existing HTML content and add footer
            # Add separator to prevent email clients from collapsing signature
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
{get_email_footer()}
</div>
</div>
</div>
</body>
</html>"""
        else:
            # Content is already wrapped, just append footer before closing body tag
            # Insert footer before </body> tag with separator to prevent collapse
            if "</body>" in content:
                footer_with_wrapper = f'<!-- Email signature separator - prevents collapse --><div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;"><div style="margin-top: 20px;">{get_email_footer()}</div></div>'
                content = content.replace("</body>", footer_with_wrapper + "</body>")
            else:
                # No body tag, just append
                content = content + f'<!-- Email signature separator - prevents collapse --><div style="margin-top: 20px; padding-top: 20px; border-top: 1px solid transparent; min-height: 1px;"><div style="margin-top: 20px;">{get_email_footer()}</div></div>'
    else:
        # Convert to HTML and add footer (this function now wraps in proper HTML structure)
        content = format_email_body_with_footer(content)
        content_type = "HTML"
    results = {"sent": [], "errors": {}}
    base = "https://graph.microsoft.com/v1.0"

    # Add invalid recipients to errors
    for invalid in invalid_recipients:
        results["errors"][invalid] = "Invalid email address format"

    for addr in valid_recipients:
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

            # Thread root
            thread_meta = {
                "subject": subject,
                "clientId": client_id_or_none,
                "email": [addr],
                "conversationId": conversation_id,
            }

            # Store row number for anchoring if provided
            if row_number:
                thread_meta["rowNumber"] = row_number

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
    doc_ref.delete()
    print(f"☠️ Moved item {doc_ref.id} to dead-letter queue: {reason}")


def send_outboxes(user_id: str, headers):
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

    outbox_ref = _fs.collection("users").document(user_id).collection("outbox")
    docs = list(outbox_ref.stream())

    if not docs:
        print("📭 Outbox empty")
        return

    print(f"📬 Found {len(docs)} outbox item(s)")

    # Group outbox items by recipient email to detect multi-property scenarios
    email_groups = defaultdict(list)
    for d in docs:
        data = d.to_dict() or {}
        emails = data.get("assignedEmails") or []
        for email in emails:
            email_lower = email.lower().strip()
            email_groups[email_lower].append({
                'doc': d,
                'data': data,
                'email': email
            })

    # Process each unique recipient
    for recipient_email, items in email_groups.items():
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
            _send_multi_property_email(user_id, headers, recipient_email, valid_items)
        else:
            # Single property - send normally
            item = valid_items[0]
            _send_single_outbox_item(user_id, headers, item)


def _send_multi_property_email(user_id: str, headers, recipient_email: str, items: list):
    """
    Send a combined email for multiple properties to the same broker.
    Creates a natural email that acknowledges all properties being inquired about.
    """
    # Extract property info from each item
    properties = []
    for item in items:
        data = item['data']
        subject = data.get("subject", "")
        # Try to extract property address from subject or script
        property_name = subject or _extract_property_from_script(data.get("script", ""))
        properties.append({
            'item': item,
            'name': property_name,
            'clientId': data.get("clientId", ""),
            'script': data.get("script", "")
        })

    # Build combined email
    primary_prop = properties[0]
    other_props = properties[1:]

    # Create natural combined script
    other_names = [p['name'] for p in other_props if p['name']]
    if other_names:
        other_list = ", ".join(other_names[:-1]) + (" and " + other_names[-1] if len(other_names) > 1 else other_names[0] if other_names else "")
        # Combine into one email - lead with the first property, mention others
        combined_script = f"""Hi,

I noticed you have several properties that caught our attention. While I'm reaching out about {primary_prop['name'] or 'one of your listings'}, I also saw you have {other_list} available.

{primary_prop['script']}

If you have any details on the other properties as well, we'd be interested in those too.

Thanks"""
    else:
        combined_script = primary_prop['script']

    first_item = items[0]
    data = first_item['data']
    clientId = (data.get("clientId") or "").strip()
    attempts = int(data.get("attempts") or 0)

    print(f"→ Sending combined email to {recipient_email} for {len(items)} properties (attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")

    try:
        res = send_and_index_email(user_id, headers, combined_script, [recipient_email], client_id_or_none=clientId)
        any_errors = bool(res["errors"])

        if not any_errors and res["sent"]:
            # Delete all outbox items for this combined send
            for item in items:
                item['doc'].reference.delete()
            print(f"🗑️ Deleted {len(items)} outbox items (combined send)")
        else:
            # Error - update attempts on all items
            new_attempts = attempts + 1
            error_msg = json.dumps(res["errors"])[:1500]

            for item in items:
                if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                    _move_to_dead_letter(user_id, item['doc'].reference, item['data'],
                        f"Send errors after {new_attempts} attempts: {error_msg}")
                else:
                    item['doc'].reference.set(
                        {"attempts": new_attempts, "lastError": error_msg},
                        merge=True,
                    )
            print(f"⚠️ Kept items with error; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")

    except Exception as e:
        new_attempts = attempts + 1
        error_msg = str(e)[:1500]

        for item in items:
            if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                _move_to_dead_letter(user_id, item['doc'].reference, item['data'],
                    f"Exception after {new_attempts} attempts: {error_msg}")
            else:
                item['doc'].reference.set(
                    {"attempts": new_attempts, "lastError": error_msg},
                    merge=True,
                )
        print(f"💥 Error sending combined email: {e}; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")


def _send_single_outbox_item(user_id: str, headers, item: dict):
    """Send a single outbox item (standard path)."""
    d = item['doc']
    data = item['data']
    emails = data.get("assignedEmails") or []
    script = data.get("script") or ""
    clientId = (data.get("clientId") or "").strip()
    attempts = int(data.get("attempts") or 0)

    print(f"→ Sending outbox item {d.id} to {len(emails)} recipient(s) (clientId={clientId or 'n/a'}, attempt {attempts + 1}/{MAX_OUTBOX_ATTEMPTS})")

    try:
        res = send_and_index_email(user_id, headers, script, emails, client_id_or_none=clientId)
        any_errors = bool(res["errors"])

        if not any_errors and res["sent"]:
            d.reference.delete()
            print(f"🗑️ Deleted outbox item {d.id}")
        else:
            new_attempts = attempts + 1
            error_msg = json.dumps(res["errors"])[:1500]

            if new_attempts >= MAX_OUTBOX_ATTEMPTS:
                _move_to_dead_letter(user_id, d.reference, data, f"Send errors after {new_attempts} attempts: {error_msg}")
            else:
                d.reference.set(
                    {"attempts": new_attempts, "lastError": error_msg},
                    merge=True,
                )
                print(f"⚠️ Kept item {d.id} with error; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")

    except Exception as e:
        new_attempts = attempts + 1
        error_msg = str(e)[:1500]

        if new_attempts >= MAX_OUTBOX_ATTEMPTS:
            _move_to_dead_letter(user_id, d.reference, data, f"Exception after {new_attempts} attempts: {error_msg}")
        else:
            d.reference.set(
                {"attempts": new_attempts, "lastError": error_msg},
                merge=True,
            )
            print(f"💥 Error sending item {d.id}: {e}; attempts={new_attempts}/{MAX_OUTBOX_ATTEMPTS}")


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
def send_email(headers, script: str, emails: list[str], client_id: str | None = None):
    """Legacy function - redirects to send_and_index_email"""
    # Note: This legacy function doesn't have user_id, so it can't use the new pipeline
    # Users should migrate to send_and_index_email directly
    raise NotImplementedError("send_email is deprecated. Use send_and_index_email with user_id parameter.")