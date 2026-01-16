import re
import requests
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter

from .clients import _fs, _get_sheet_id_or_fail, _sheets_client
from .sheets import format_sheet_columns_autosize_with_exceptions, _get_first_tab_title, _read_header_row2, append_links_to_flyer_link_column, _header_index_map, _find_row_by_email
from .sheet_operations import _find_row_by_anchor, ensure_nonviable_divider, move_row_below_divider, insert_property_row_above_divider, _is_row_below_nonviable
from .messaging import (save_message, save_thread_root, index_message_id, index_conversation_id,
                       dump_thread_from_firestore, has_processed, mark_processed, set_last_scan_iso,
                       lookup_thread_by_message_id, lookup_thread_by_conversation_id)
from .logging import write_message_order_test
from .ai_processing import propose_sheet_updates, apply_proposal_to_sheet, get_row_anchor, check_missing_required_fields
from .file_handling import fetch_and_process_pdfs, upload_pdf_to_drive
from .notifications import write_notification, add_client_notifications
from .utils import (exponential_backoff_request, strip_html_tags, safe_preview, 
                   parse_references_header, normalize_message_id, fetch_url_as_text, _sanitize_url,
                   format_email_body_with_footer)
from .email_operations import (
    send_remaining_questions_email, 
    send_closing_email,
    send_thankyou_closing_with_new_property,
    send_thankyou_ask_alternatives
)
from .app_config import REQUIRED_FIELDS_FOR_CLOSE, INBOX_SCAN_WINDOW_HOURS

def _store_contact_optout(user_id: str, email: str, reason: str, thread_id: str) -> bool:
    """
    Store a contact's opt-out status in Firestore.
    This prevents future emails from being sent to this contact.
    """
    try:
        import hashlib
        from google.cloud.firestore import SERVER_TIMESTAMP

        # Use email hash as document ID for consistent lookups
        email_lower = email.lower().strip()
        email_hash = hashlib.sha256(email_lower.encode('utf-8')).hexdigest()[:16]

        optout_ref = _fs.collection("users").document(user_id).collection("optedOutContacts").document(email_hash)

        optout_ref.set({
            "email": email_lower,
            "reason": reason,
            "optedOutAt": SERVER_TIMESTAMP,
            "threadId": thread_id
        })

        print(f"📝 Stored opt-out for {email_lower} (reason: {reason})")
        return True

    except Exception as e:
        print(f"⚠️ Failed to store opt-out for {email}: {e}")
        return False


def is_contact_opted_out(user_id: str, email: str) -> dict | None:
    """
    Check if a contact has opted out of communications.
    Returns the opt-out record if found, None otherwise.
    """
    try:
        import hashlib

        email_lower = email.lower().strip()
        email_hash = hashlib.sha256(email_lower.encode('utf-8')).hexdigest()[:16]

        optout_ref = _fs.collection("users").document(user_id).collection("optedOutContacts").document(email_hash)
        doc = optout_ref.get()

        if doc.exists:
            return doc.to_dict()
        return None

    except Exception as e:
        print(f"⚠️ Failed to check opt-out status for {email}: {e}")
        return None


def send_reply_in_thread(user_id: str, headers: dict, body: str, current_msg_id: str, recipient: str, thread_id: str) -> bool:
    """Send a reply to the current message being processed and index it for future replies"""
    try:
        from .utils import exponential_backoff_request, safe_preview
        from .messaging import save_message, index_message_id, index_conversation_id, lookup_thread_by_message_id
        from datetime import datetime, timezone
        import requests
        import time
        
        base = "https://graph.microsoft.com/v1.0"
        
        # Format body as HTML with footer
        html_body = format_email_body_with_footer(body)
        
        # Reply directly to the current message we're processing
        # Use message structure to preserve line breaks properly
        reply_payload = {
            "message": {
                "body": {
                    "contentType": "HTML",
                    "content": html_body
                }
            }
        }
        resp = exponential_backoff_request(
            lambda: requests.post(f"{base}/me/messages/{current_msg_id}/reply",
                                 headers=headers, json=reply_payload, timeout=30)
        )
        
        # Verify successful response
        if resp and resp.status_code in [200, 201, 202]:
            print(f"   ✅ Sent reply to current message via /reply endpoint")
            
            # CRITICAL: Index the sent message so future replies can find the thread
            # The /reply endpoint doesn't return the message ID, so we need to fetch it from SentItems
            try:
                # Wait a moment for the message to appear in SentItems
                time.sleep(1)
                
                # Fetch the most recent message from SentItems for this conversation
                # Get conversationId from the current message
                current_msg_resp = exponential_backoff_request(
                    lambda: requests.get(
                        f"{base}/me/messages/{current_msg_id}",
                        headers=headers,
                        params={"$select": "conversationId"},
                        timeout=30
                    )
                )
                conversation_id = current_msg_resp.json().get("conversationId") if current_msg_resp.status_code == 200 else None
                
                if conversation_id:
                    # Fetch recent sent messages in this conversation
                    sent_resp = exponential_backoff_request(
                        lambda: requests.get(
                            f"{base}/me/mailFolders/SentItems/messages",
                            headers=headers,
                            params={
                                "$filter": f"conversationId eq '{conversation_id}'",
                                "$orderby": "sentDateTime desc",
                                "$top": 1,
                                "$select": "id,internetMessageId,conversationId,subject,toRecipients,sentDateTime,body,bodyPreview"
                            },
                            timeout=30
                        )
                    )
                    
                    if sent_resp.status_code == 200:
                        sent_messages = sent_resp.json().get("value", [])
                        if sent_messages:
                            sent_msg = sent_messages[0]  # Most recent
                            sent_internet_msg_id = sent_msg.get("internetMessageId")
                            
                            if sent_internet_msg_id:
                                # Index this sent message with retry logic
                                normalized_id = normalize_message_id(sent_internet_msg_id)

                                # Retry indexing up to 3 times
                                MAX_RETRIES = 3
                                msg_indexed = False
                                for attempt in range(MAX_RETRIES):
                                    if index_message_id(user_id, sent_internet_msg_id, thread_id):
                                        # Verify the index was written
                                        time.sleep(0.2)
                                        if lookup_thread_by_message_id(user_id, sent_internet_msg_id) == thread_id:
                                            msg_indexed = True
                                            break
                                    print(f"   ⚠️ Reply index attempt {attempt + 1}/{MAX_RETRIES} failed, retrying...")
                                    time.sleep(0.5 * (attempt + 1))

                                if not msg_indexed:
                                    print(f"   ⚠️ CRITICAL: Failed to index reply after {MAX_RETRIES} attempts - future replies may be orphaned")
                                    # SAFETY: Return failure because email was sent but not indexed
                                    # Caller should be aware that conversation tracking is broken
                                    return False

                                # Also save the message record
                                to_recipients = [r.get("emailAddress", {}).get("address", "") for r in sent_msg.get("toRecipients", [])]
                                body_obj = sent_msg.get("body", {}) or {}
                                body_content = body_obj.get("content", "")

                                message_record = {
                                    "direction": "outbound",
                                    "subject": sent_msg.get("subject", ""),
                                    "from": "me",
                                    "to": to_recipients,
                                    "sentDateTime": sent_msg.get("sentDateTime"),
                                    "receivedDateTime": None,
                                    "headers": {
                                        "internetMessageId": sent_internet_msg_id,
                                        "inReplyTo": None,  # Would need to extract from headers
                                        "references": []
                                    },
                                    "body": {
                                        "contentType": body_obj.get("contentType", "HTML"),
                                        "content": body_content,
                                        "preview": sent_msg.get("bodyPreview", "")[:200] or safe_preview(body_content)
                                    }
                                }
                                save_message(user_id, thread_id, normalized_id, message_record)

                                # Index conversation ID with retry
                                if conversation_id:
                                    for attempt in range(MAX_RETRIES):
                                        if index_conversation_id(user_id, conversation_id, thread_id):
                                            break
                                        time.sleep(0.5 * (attempt + 1))

                                print(f"   📝 Indexed sent reply message: {sent_internet_msg_id[:50]}...")
                            else:
                                print(f"   ⚠️ Sent message has no internetMessageId, cannot index")
                        else:
                            print(f"   ⚠️ Could not find sent message in SentItems to index")
                    else:
                        print(f"   ⚠️ Failed to fetch sent message: {sent_resp.status_code}")
                else:
                    print(f"   ⚠️ Could not get conversationId to index sent message")
            except Exception as e:
                print(f"   ⚠️ Failed to index sent reply (non-fatal): {e}")
            
            return True
        else:
            print(f"   ❌ Reply failed with status {resp.status_code if resp else 'None'}")
            # Fallback: send a new email with proper threading headers
            msg = {
                "subject": "Re: Property information",
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
                "internetMessageHeaders": [
                    {"name": "In-Reply-To", "value": thread_id},
                    {"name": "References", "value": thread_id}
                ]
            }
            send_payload = {"message": msg, "saveToSentItems": True}
            resp = exponential_backoff_request(
                lambda: requests.post(f"{base}/me/sendMail", headers=headers, 
                                     json=send_payload, timeout=30)
            )
            
            # Verify successful response
            if resp and resp.status_code in [200, 201, 202]:
                print(f"   ✅ Sent reply via /sendMail with threading headers")
                # Note: sendMail also needs indexing, but that's more complex - would need to fetch from SentItems too
                return True
            else:
                print(f"   ❌ SendMail failed with status {resp.status_code if resp else 'None'}")
                return False
        
    except Exception as e:
        print(f"   ❌ Failed to send reply: {e}")
        return False

def _find_client_id_by_email(uid: str, email: str) -> str | None:
    """
    Search through all clients (active and archived) to find which one has a sheet
    with a row matching the given email address.
    Returns clientId if found, None otherwise.
    """
    if not email:
        return None
    
    email_lower = email.lower().strip()
    
    try:
        # Search active clients
        clients_ref = _fs.collection("users").document(uid).collection("clients")
        clients = list(clients_ref.stream())
        
        for client_doc in clients:
            client_id = client_doc.id
            client_data = client_doc.to_dict() or {}
            sheet_id = client_data.get("sheetId")
            
            if not sheet_id:
                continue
            
            try:
                # Try to find email in this client's sheet
                sheets = _sheets_client()
                tab_title = _get_first_tab_title(sheets, sheet_id)
                header = _read_header_row2(sheets, sheet_id, tab_title)
                rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab_title, header, email_lower)
                
                if rownum is not None:
                    print(f"   ✅ Found email {email_lower} in client {client_id}, sheet {sheet_id}, row {rownum}")
                    return client_id
            except Exception as e:
                # Skip this client if sheet access fails
                continue
        
        # Search archived clients
        archived_clients_ref = _fs.collection("users").document(uid).collection("archivedClients")
        archived_clients = list(archived_clients_ref.stream())
        
        for client_doc in archived_clients:
            client_id = client_doc.id
            client_data = client_doc.to_dict() or {}
            sheet_id = client_data.get("sheetId")
            
            if not sheet_id:
                continue
            
            try:
                # Try to find email in this archived client's sheet
                sheets = _sheets_client()
                tab_title = _get_first_tab_title(sheets, sheet_id)
                header = _read_header_row2(sheets, sheet_id, tab_title)
                rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab_title, header, email_lower)
                
                if rownum is not None:
                    print(f"   ✅ Found email {email_lower} in archived client {client_id}, sheet {sheet_id}, row {rownum}")
                    return client_id
            except Exception as e:
                # Skip this client if sheet access fails
                continue
        
        return None
    except Exception as e:
        print(f"   ⚠️ Failed to search clients for email {email_lower}: {e}")
        return None

def fetch_and_log_sheet_for_thread(uid: str, thread_id: str, counterparty_email: str | None):
    # Read thread (to get clientId)
    tdoc = (_fs.collection("users").document(uid)
            .collection("threads").document(thread_id).get())
    if not tdoc.exists:
        print("⚠️ Thread doc not found; cannot fetch sheet")
        return None, None, None, None, None  # Return tuple for unpacking

    tdata = tdoc.to_dict() or {}
    client_id = tdata.get("clientId")
    if not client_id:
        print("⚠️ Thread has no clientId; cannot fetch sheet")
        return None, None, None, None, None

    # Required: sheetId on client doc
    try:
        sheet_id = _get_sheet_id_or_fail(uid, client_id)
    except RuntimeError as e:
        print(str(e))
        return None, None, None, None, None

    # Counterparty email fallback: use thread's stored recipients if missing
    if not counterparty_email:
        recips = tdata.get("email") or []
        if recips:
            counterparty_email = recips[0]

    # Connect to Sheets; header = row 2
    sheets = _sheets_client()
    tab_title = _get_first_tab_title(sheets, sheet_id)
    header = _read_header_row2(sheets, sheet_id, tab_title)

    # Ensure sizing/behavior is correct on every run (idempotent)
    format_sheet_columns_autosize_with_exceptions(sheet_id, header)

    print(f"📄 Sheet fetched: title='{tab_title}', sheetId={sheet_id}")
    print(f"   Header (row 2): {header}")
    print(f"   Counterparty email (row match): {counterparty_email or 'unknown'}")

    # NEW: Use row anchoring for enhanced row matching
    rownum, rowvals = _find_row_by_anchor(uid, thread_id, sheets, sheet_id, tab_title, header, counterparty_email or "")
    
    if rownum is not None:
        print(f"📌 Matched row {rownum}: {rowvals}")
        return client_id, sheet_id, header, rownum, rowvals
    else:
        # Be loud – row must exist for our workflow
        print(f"❌ No sheet row found with email = {counterparty_email}")
        return client_id, sheet_id, header, None, None

def process_inbox_message(user_id: str, headers: Dict[str, str], msg: Dict[str, Any]):
    """ENHANCED: Process a single inbox message with full pipeline including events."""
    msg_id = msg.get("id")
    subject = msg.get("subject", "")
    from_info = msg.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    from_name = from_info.get("name", "")  # Extract sender name from email
    internet_message_id = msg.get("internetMessageId")
    conversation_id = msg.get("conversationId")
    received_dt = msg.get("receivedDateTime")
    sent_dt = msg.get("sentDateTime")
    body_preview = msg.get("bodyPreview", "")
    
    # NEW: fetch full message body and normalize to plain text
    try:
        full_body_resp = exponential_backoff_request(
            lambda: requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}",
                headers=headers,
                params={"$select": "body"},
                timeout=30
            )
        ).json().get("body", {}) or {}
        _raw_content = full_body_resp.get("content", "") or ""
        _ctype = (full_body_resp.get("contentType") or "Text").upper()
        _full_text = strip_html_tags(_raw_content) if _ctype == "HTML" else _raw_content
    except Exception as e:
        print(f"⚠️ Could not fetch full body for {msg_id}: {e}")
        _full_text = body_preview or ""

    to_recipients = [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])]
    
    # Get headers if not present
    internet_message_headers = msg.get("internetMessageHeaders")
    if not internet_message_headers:
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}",
                    headers=headers,
                    params={"$select": "internetMessageHeaders"},
                    timeout=30
                )
            )
            internet_message_headers = response.json().get("internetMessageHeaders", [])
        except Exception as e:
            print(f"⚠️ Could not fetch headers for {msg_id}: {e}")
            internet_message_headers = []
    
    # Extract reply headers and check for auto-replies
    in_reply_to = None
    references = []
    is_auto_reply = False

    for header in internet_message_headers or []:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "in-reply-to":
            in_reply_to = normalize_message_id(value)
        elif name == "references":
            references = parse_references_header(value)
        # Detect auto-reply headers (RFC 3834)
        elif name == "auto-submitted" and value.lower() != "no":
            is_auto_reply = True
        elif name == "x-auto-response-suppress":
            is_auto_reply = True
        elif name == "x-autoreply" or name == "x-autorespond":
            is_auto_reply = True
        elif name == "precedence" and value.lower() in ["bulk", "junk", "auto_reply"]:
            is_auto_reply = True

    # Also check subject line for common auto-reply patterns
    subject_lower = subject.lower()
    auto_reply_subjects = [
        "out of office", "automatic reply", "auto-reply", "auto reply",
        "autoreply", "away from office", "on vacation", "ooo:",
        "automatische antwort", "réponse automatique"  # German, French
    ]
    if any(pattern in subject_lower for pattern in auto_reply_subjects):
        is_auto_reply = True

    # SAFETY: Skip auto-replies to prevent processing OOO messages as real data
    if is_auto_reply:
        print(f"⏭️ Skipping auto-reply from {from_addr}: {subject}")
        print(f"   Auto-reply emails are not processed to prevent data corruption")
        return

    print(f"📧 Processing: {subject} from {from_addr}")
    print(f"   In-Reply-To: {in_reply_to}")
    print(f"   References: {references}")
    
    # Match against our index
    thread_id = None
    matched_header = None
    
    # Try In-Reply-To first
    if in_reply_to:
        thread_id = lookup_thread_by_message_id(user_id, in_reply_to)
        if thread_id:
            matched_header = f"In-Reply-To: {in_reply_to}"
    
    # Try References (newest to oldest)
    if not thread_id and references:
        for ref in reversed(references):  # References are oldest to newest, we want newest first
            ref = normalize_message_id(ref)
            thread_id = lookup_thread_by_message_id(user_id, ref)
            if thread_id:
                matched_header = f"References: {ref}"
                break
    
    # Fallback to conversation ID
    if not thread_id and conversation_id:
        thread_id = lookup_thread_by_conversation_id(user_id, conversation_id)
        if thread_id:
            matched_header = f"ConversationId: {conversation_id}"
    
    # If no thread match found, this is a NEW conversation we didn't start - ignore it
    # Only process emails that are actual replies to messages we sent
    # (matched via In-Reply-To, References, or indexed conversationId)
    if not thread_id:
        print(f"⏭️ Ignoring email from {from_addr} - not a reply to any tracked thread")
        print(f"   Subject: {subject}")
        print(f"   ConversationId: {conversation_id} (not in our index)")
        return
    
    print(f"🎯 Matched via {matched_header} -> thread {thread_id}")
    
    # Create message record
    message_record = {
        "direction": "inbound",
        "subject": subject,
        "from": from_addr,
        "to": to_recipients,
        "sentDateTime": sent_dt,
        "receivedDateTime": received_dt,
        "headers": {
            "internetMessageId": internet_message_id,
            "inReplyTo": in_reply_to,
            "references": references
        },
        "body": {
            "contentType": "Text",
            "content": _full_text,
            "preview": safe_preview(_full_text)
        }
    }
    
    # Save to Firestore with retry logic for reliability
    import time
    MAX_RETRIES = 3

    if internet_message_id:
        # Save message with retry
        for attempt in range(MAX_RETRIES):
            if save_message(user_id, thread_id, internet_message_id, message_record):
                break
            print(f"⚠️ Inbound message save attempt {attempt + 1}/{MAX_RETRIES} failed, retrying...")
            time.sleep(0.5 * (attempt + 1))

        # Index with retry and verification
        msg_indexed = False
        for attempt in range(MAX_RETRIES):
            if index_message_id(user_id, internet_message_id, thread_id):
                time.sleep(0.2)
                if lookup_thread_by_message_id(user_id, internet_message_id) == thread_id:
                    msg_indexed = True
                    break
            print(f"⚠️ Inbound message index attempt {attempt + 1}/{MAX_RETRIES} failed, retrying...")
            time.sleep(0.5 * (attempt + 1))

        if not msg_indexed:
            print(f"⚠️ Failed to index inbound message after {MAX_RETRIES} attempts")
    else:
        # Use Graph message ID as fallback
        save_message(user_id, thread_id, msg_id, message_record)
    
    # Update thread timestamp
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_ref.set({"updatedAt": SERVER_TIMESTAMP}, merge=True)
    except Exception as e:
        print(f"⚠️ Failed to update thread timestamp: {e}")
    
    # Dump the conversation
    dump_thread_from_firestore(user_id, thread_id)
    
    # Step 1: fetch Google Sheet (required) and log header + counterparty email
    client_id, sheet_id, header, rownum, rowvals = fetch_and_log_sheet_for_thread(user_id, thread_id, counterparty_email=from_addr)
    
    # If no clientId found, try to find it by email and update the thread
    if not client_id and from_addr:
        print(f"   🔍 Retrying clientId lookup for email: {from_addr}")
        client_id = _find_client_id_by_email(user_id, from_addr)
        if client_id:
            print(f"   ✅ Found clientId: {client_id}, updating thread...")
            # Update thread with clientId
            thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
            thread_ref.set({"clientId": client_id}, merge=True)
            # Retry fetching sheet
            client_id, sheet_id, header, rownum, rowvals = fetch_and_log_sheet_for_thread(user_id, thread_id, counterparty_email=from_addr)
    
    # Extract contact name: try email name first, then sheet row
    contact_name = None
    if from_name:
        contact_name = from_name.strip()
        print(f"📝 Extracted name from email: {contact_name}")
    
    # Try to get name from sheet row (common column names)
    if not contact_name and rowvals and header:
        idx_map = _header_index_map(header)
        name_keys = ["name", "contact name", "leasing contact", "contact", "broker name", "broker"]
        for key in name_keys:
            idx = idx_map.get(key)
            if idx and (idx - 1) < len(rowvals):
                name_val = (rowvals[idx - 1] or "").strip()
                if name_val:
                    contact_name = name_val
                    print(f"📝 Extracted name from sheet column '{key}': {contact_name}")
                    break
    
    # Only proceed if we successfully matched a sheet row
    if sheet_id and rownum is not None:
        # Get the correct recipient email from the thread metadata (original external contact)
        # The send_and_index_email function will handle threading properly
        try:
            # Get thread participants to find the external contact
            thread_doc = _fs.collection("users").document(user_id).collection("threads").document(thread_id).get()
            thread_data = thread_doc.to_dict() or {}
            thread_emails = thread_data.get("email", [])

            # Find the external contact email using header mapping (NOT hardcoded index)
            # SAFETY: Use column header lookup instead of hardcoded position
            external_email = None
            if rowvals and header:
                # Build header index map for reliable column lookup
                idx_map = _header_index_map(header)
                # Try common email column names
                email_col_names = ["email", "email address", "contact email", "leasing email"]
                for col_name in email_col_names:
                    if col_name in idx_map:
                        email_idx = idx_map[col_name] - 1  # Convert to 0-based
                        if 0 <= email_idx < len(rowvals):
                            sheet_email = (rowvals[email_idx] or "").strip().lower()
                            if sheet_email and "@" in sheet_email:
                                external_email = sheet_email
                                break

            # Fallback: use thread participants if sheet email not found
            if not external_email and thread_emails:
                external_email = thread_emails[0].lower()

            # Final fallback: use current sender (with warning)
            recipient_email = external_email or (from_addr or "").lower()
            if not external_email:
                print(f"⚠️ Could not find email in sheet row, falling back to sender")

            print(f"📧 Reply recipient determined: {recipient_email}")
            print(f"   Thread participants: {thread_emails}")
            print(f"   Sheet email column found: {'Yes' if external_email else 'No'}")
            print(f"   Will reply to: {recipient_email}")
            
        except Exception as e:
            print(f"⚠️ Could not determine thread recipient, using current sender: {e}")
            recipient_email = (from_addr or "").lower()
        
        # Keep the original variable name for compatibility but use correct recipient
        from_addr_lower = recipient_email

        # --- flags for gating later ---
        old_row_became_nonviable = False   # set true when we move the row below divider
        new_row_created = False            # set true when we insert a new property row
        new_row_number = None              # track the newly created row number

        # NEW: Handle PDF attachments with enhanced extraction for current message only
        pdf_manifest = fetch_and_process_pdfs(headers, msg_id)

        if pdf_manifest:
            # Collect drive links for sheet
            drive_links = [p['drive_link'] for p in pdf_manifest if p.get('drive_link')]

            # Append Drive links to Flyer / Link column on the current row
            if drive_links:
                try:
                    sheets = _sheets_client()
                    append_links_to_flyer_link_column(sheets, sheet_id, header, rownum, drive_links)
                    # Re-read header in case we just created "Flyer / Link"
                    try:
                        tab_title = _get_first_tab_title(sheets, sheet_id)
                        header = _read_header_row2(sheets, sheet_id, tab_title)
                        format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                    except Exception as _e:
                        print(f"ℹ️ Skipped re-format after link append: {_e}")

                except Exception as e:
                    print(f"❌ Failed to append links to sheet: {e}")
        
        # URL exploration - find URLs in message and fetch content for AI processing only
        url_texts = []
        url_pattern = r'https?://[^\s<>"\']+'
        urls_found = re.findall(url_pattern, _full_text)
        
        for url in urls_found[:3]:  # Limit to 3 URLs to avoid overwhelming
            clean = _sanitize_url(url)
            fetched_text = fetch_url_as_text(clean)
            if fetched_text:
                url_texts.append({"url": clean, "text": fetched_text})
        
        # Step 2: test write
        write_message_order_test(user_id, thread_id, sheet_id)
        
        # Step 3: get proposal using Responses API with URL content and PDF data
        proposal = propose_sheet_updates(
            user_id, client_id, from_addr_lower, sheet_id, header, rownum, rowvals,
            thread_id, pdf_manifest=pdf_manifest, url_texts=url_texts, contact_name=contact_name,
            headers=headers
        )
        
        if proposal:
            # Process updates
            if proposal.get("updates"):
                apply_result = apply_proposal_to_sheet(
                    user_id, client_id, sheet_id, header, rownum, rowvals, proposal
                )

                # Store applied record in sheetChangeLog
                try:
                    applied_hash = hashlib.sha256(
                        json.dumps(apply_result, sort_keys=True).encode("utf-8")
                    ).hexdigest()[:16]

                    from datetime import datetime as dt, timezone as tz
                    now_id = dt.now(tz.utc).isoformat().replace(":", "-").replace(".", "-").replace("+00:00", "Z")
                    _fs.collection("users").document(user_id).collection("sheetChangeLog").document(f"{thread_id}__applied__{now_id}").set({
                        "clientId": client_id,
                        "email": from_addr_lower,
                        "sheetId": sheet_id,
                        "rowNumber": rownum,
                        "applied": apply_result,
                        "status": "applied",
                        "threadId": thread_id,
                        "createdAt": SERVER_TIMESTAMP,
                        "fileIds": file_ids_for_this_run,
                        "proposalHash": applied_hash,
                    })
                except Exception as e:
                    print(f"⚠️ Failed to store applied record: {e}")

                # Write client notifications (one per field)
                add_client_notifications(
                    user_id, client_id, from_addr_lower, thread_id,
                    applied_updates=apply_result.get("applied", []),
                    notes=proposal.get("notes")
                )
            
            # Process events from the proposal
            sheets = _sheets_client()
            row_anchor = get_row_anchor(rowvals, header)
            
            events = proposal.get("events", [])
            for event in events:
                event_type = event.get("type")
                
                if event_type == "call_requested":
                    # Check if phone number is mentioned in the message
                    phone_pattern = r'(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})'
                    phone_match = re.search(phone_pattern, _full_text)
                    phone_number = phone_match.group(0) if phone_match else None
                    
                    # Create action_needed notification
                    try:
                        meta = {
                            "reason": "call_requested",
                            "details": "Call requested in conversation"
                        }
                        if phone_number:
                            meta["phoneNumber"] = phone_number
                            meta["details"] = f"Call requested - phone number provided: {phone_number}"
                        
                        write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=from_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta=meta,
                            dedupe_key=f"call_requested:{thread_id}"
                        )
                        print(f"📞 Created call_requested notification" + (f" with phone: {phone_number}" if phone_number else ""))
                        
                        # If phone number is provided, skip email response (just notification)
                        # If no phone number, we'll send a brief response asking for it
                        if phone_number:
                            print(f"📞 Phone number found - skipping email response, notification only")
                            # Mark that we should skip the normal email response
                            proposal["skip_response"] = True
                    except Exception as e:
                        print(f"❌ Failed to write call_requested notification: {e}")

                elif event_type == "needs_user_input":
                    # Client asked a question or made a request the AI cannot handle
                    # Create notification and skip auto-response
                    try:
                        reason = event.get("reason", "unclear")
                        question = event.get("question", "User input required")

                        reason_labels = {
                            "client_question": "Client asked about your requirements",
                            "scheduling": "Tour/meeting scheduling request",
                            "negotiation": "Price or term negotiation",
                            "confidential": "Asked about client identity",
                            "legal_contract": "Contract or legal question",
                            "unclear": "Message needs your review"
                        }

                        meta = {
                            "reason": f"needs_user_input:{reason}",
                            "details": reason_labels.get(reason, reason_labels["unclear"]),
                            "question": question,
                            "originalMessage": _full_text[:500]  # Include message context
                        }

                        write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=from_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta=meta,
                            dedupe_key=f"needs_user_input:{thread_id}:{reason}"
                        )
                        print(f"⚠️ Created needs_user_input notification (reason: {reason})")

                        # Always skip auto-response when user input is needed
                        proposal["skip_response"] = True

                    except Exception as e:
                        print(f"❌ Failed to write needs_user_input notification: {e}")

                elif event_type == "property_unavailable":
                    # Check if row is already below NON-VIABLE divider - if so, skip processing
                    try:
                        tab_title = _get_first_tab_title(sheets, sheet_id)
                        if _is_row_below_nonviable(sheets, sheet_id, tab_title, rownum):
                            print(f"ℹ️ Row {rownum} already below NON-VIABLE divider, skipping property_unavailable processing")
                            continue
                    except Exception as e:
                        print(f"⚠️ Failed to check if row is below divider: {e}")
                        # Continue processing if we can't determine position
                    
                    # Move row below divider and create notification
                    message_content = _full_text.lower()
                    unavailable_keywords = [
                        "no longer available", "not available", "off the market", 
                        "has been leased", "space is leased", "property is unavailable",
                        "building unavailable", "no longer considering", "isnt available", 
                        "isn't available", "unavailable", "off market"
                    ]
                    
                    # Only proceed if we find explicit unavailability language
                    if any(keyword in message_content for keyword in unavailable_keywords):
                        try:
                            # Find which keyword triggered the unavailability detection
                            found_keyword = next(keyword for keyword in unavailable_keywords if keyword in message_content)
                            
                            divider_row = ensure_nonviable_divider(sheets, sheet_id, tab_title)
                            new_rownum = move_row_below_divider(sheets, sheet_id, tab_title, rownum, divider_row)
                            
                            # Add comment to "Jill and Clients comments" column explaining why it was marked unviable
                            try:
                                # Find the comments column index
                                comments_col_idx = None
                                for i, col_name in enumerate(header):
                                    if col_name and "jill and clients comments" in col_name.lower():
                                        comments_col_idx = i + 1  # 1-based for Sheets API
                                        break
                                
                                if comments_col_idx:
                                    # Get current date for the comment
                                    from datetime import datetime
                                    current_date = datetime.now().strftime("%m/%d/%Y")
                                    
                                    # Create comment explaining why property was marked unviable
                                    unavailable_comment = f"[{current_date}] Property marked unavailable - contact said: '{found_keyword}'"
                                    
                                    # Get existing comments to append to them
                                    existing_resp = sheets.spreadsheets().values().get(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}"
                                    ).execute()
                                    existing_comment = ""
                                    if existing_resp.get("values"):
                                        existing_comment = existing_resp["values"][0][0] if existing_resp["values"][0] else ""
                                    
                                    # Combine existing and new comments
                                    if existing_comment.strip():
                                        final_comment = f"{existing_comment.strip()} | {unavailable_comment}"
                                    else:
                                        final_comment = unavailable_comment
                                    
                                    # Update the comments cell
                                    sheets.spreadsheets().values().update(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}",
                                        valueInputOption="RAW",
                                        body={"values": [[final_comment]]}
                                    ).execute()
                                    
                                    print(f"💬 Added unavailability comment: {unavailable_comment}")
                                else:
                                    print(f"⚠️ Could not find 'Jill and Clients comments' column to add unavailability reason")
                            except Exception as comment_error:
                                print(f"⚠️ Failed to add unavailability comment: {comment_error}")
                            
                            # Reformat after move
                            format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                            
                            # mark the row as non-viable for this run
                            old_row_became_nonviable = True
                            rownum = new_rownum  # keep our pointer accurate if used later

                            # Create notification only after successful move
                            write_notification(
                                user_id, client_id,
                                kind="property_unavailable",
                                priority="important",
                                email=from_addr_lower,
                                thread_id=thread_id,
                                row_number=new_rownum,
                                row_anchor=row_anchor,
                                meta={"address": event.get("address", ""), "city": event.get("city", "")},
                                dedupe_key=f"property_unavailable:{thread_id}:{new_rownum}:moved"
                            )
                            print(f"🚫 Moved property to non-viable and created notification")
                        except Exception as e:
                            print(f"❌ Failed to handle property_unavailable: {e}")
                    else:
                        print(f"⚠️ Property unavailable event detected but no explicit unavailability keywords found")

                elif event_type == "new_property":
                    try:
                        address = event.get("address", "")
                        city = event.get("city", "")
                        
                        # Skip if no address provided
                        if not address or not address.strip():
                            print("⚠️ No address provided for new_property event, skipping")
                            continue
                        
                        address = address.strip()
                        city = city.strip() if city else ""
                        
                        # Check if property already exists in sheet
                        tab_title = _get_first_tab_title(sheets, sheet_id)
                        resp = sheets.spreadsheets().values().get(
                            spreadsheetId=sheet_id,
                            range=f"{tab_title}!3:1000"  # Skip header rows, read data rows
                        ).execute()
                        
                        existing_rows = resp.get("values", [])
                        property_exists = False
                        
                        # Build header index map to find address/city columns
                        idx_map = _header_index_map(header)
                        addr_col = idx_map.get("property address") or idx_map.get("address")
                        city_col = idx_map.get("city")
                        
                        if addr_col is not None:
                            # Check each row for existing property
                            for row_idx, row in enumerate(existing_rows, start=3):
                                if len(row) > (addr_col - 1):  # -1 because idx_map is 1-based
                                    existing_addr = (row[addr_col - 1] or "").strip().lower()
                                    existing_city = ""
                                    
                                    if city_col is not None and len(row) > (city_col - 1):
                                        existing_city = (row[city_col - 1] or "").strip().lower()
                                    
                                    # Match both address and city
                                    if (existing_addr == address.lower() and 
                                        existing_city == city.lower()):
                                        property_exists = True
                                        print(f"ℹ️ Property '{address}, {city}' already exists in row {row_idx}, skipping")
                                        break
                        
                        if property_exists:
                            continue  # Skip this event - property already exists
                        
                        # Property doesn't exist, proceed with creation
                        link = event.get("link", "")
                        notes = event.get("notes", "")
                        
                        # Prepare values for new row
                        values_by_header = {}
                        if address:
                            values_by_header["property address"] = address
                            values_by_header["address"] = address
                        if city:
                            values_by_header["city"] = city
                        if from_addr_lower:
                            values_by_header["email"] = from_addr_lower
                            values_by_header["email address"] = from_addr_lower
                        
                        # Copy leasing company and contact from current row
                        leasing_company_idx = idx_map.get("leasing company") or idx_map.get("leasing company ")
                        leasing_contact_idx = idx_map.get("leasing contact") 
                        
                        if leasing_company_idx and (leasing_company_idx - 1) < len(rowvals):
                            leasing_company = rowvals[leasing_company_idx - 1]
                            if leasing_company:
                                values_by_header["leasing company"] = leasing_company
                                values_by_header["leasing company "] = leasing_company
                        
                        if leasing_contact_idx and (leasing_contact_idx - 1) < len(rowvals):
                            leasing_contact = rowvals[leasing_contact_idx - 1]
                            if leasing_contact:
                                values_by_header["leasing contact"] = leasing_contact
                        
                        # Put the URL itself in Flyer / Link initially
                        if link:
                            values_by_header["flyer / link"] = link

                        # Keep human-readable notes (without the URL) in Listing Brokers Comments 
                        if notes:
                            values_by_header["listing brokers comments"] = notes

                        new_rownum = insert_property_row_above_divider(sheets, sheet_id, tab_title, values_by_header)

                        # Reformat after insert
                        format_sheet_columns_autosize_with_exceptions(sheet_id, header)

                        # remember the new row to target links later
                        new_row_created = True
                        new_row_number = new_rownum

                        # Build suggested (not sent) email payload
                        email_payload = {
                            "to": [from_addr_lower],
                            "subject": f"{address}, {city}" if city else address,
                            "body": f"""Hi,

You mentioned a new property: {address}{', ' + city if city else ''}.

If you think this might be a good fit:
> Can you please verify the current asking rent rates and NNN's?
> Provide any floor plans or flyers you may have.
> When will the space be available?

Just like before — if this one’s no longer available or not a fit, feel free to let me know so I can cross it off and stop bugging you. And of course, if you know of any others that might be a good fit, I’d love to hear about them.

Thanks!""",
                            "clientId": client_id,
                            "rowNumber": new_rownum
                        }

                        # Create ACTION_NEEDED notification with draft email
                        write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=from_addr_lower,
                            thread_id=thread_id,   # keep context with original thread
                            row_number=new_rownum,
                            row_anchor=f"{address}, {city}" if city else address,
                            meta={
                                "reason": "new_property_pending_send",
                                "status": "pending_send",
                                "address": address,
                                "city": city,
                                "link": link,
                                "notes": notes,
                                "suggestedEmail": email_payload
                            },
                            dedupe_key=f"new_property_pending:{thread_id}:{address}:{city}:{from_addr_lower}"
                        )
                        print(f"🏢 Created new property row and pending notification")

                    except Exception as e:
                        print(f"❌ Failed to handle new_property: {e}")
                
                elif event_type == "close_conversation":
                    # This will be handled below in the required fields check
                    print(f"💬 Close conversation event detected")

                elif event_type == "contact_optout":
                    # Contact explicitly doesn't want further communication
                    try:
                        reason = event.get("reason", "not_interested")

                        reason_labels = {
                            "not_interested": "Contact is not interested",
                            "unsubscribe": "Contact requested to be removed from mailing list",
                            "do_not_contact": "Contact requested no further contact",
                            "no_tenant_reps": "Contact doesn't work with tenant rep brokers",
                            "direct_only": "Contact only deals directly with tenants",
                            "hostile": "Contact responded negatively - requires review"
                        }

                        # Store opt-out in Firestore for future reference
                        _store_contact_optout(user_id, from_addr_lower, reason, thread_id)

                        # Move row to NON-VIABLE with reason
                        try:
                            tab_title = _get_first_tab_title(sheets, sheet_id)
                            if not _is_row_below_nonviable(sheets, sheet_id, tab_title, rownum):
                                divider_row = ensure_nonviable_divider(sheets, sheet_id, tab_title)
                                new_rownum = move_row_below_divider(sheets, sheet_id, tab_title, rownum, divider_row)

                                # Add comment explaining why
                                from datetime import datetime
                                current_date = datetime.now().strftime("%m/%d/%Y")
                                optout_comment = f"[{current_date}] Contact opted out: {reason_labels.get(reason, reason)}"

                                # Find comments column
                                comments_col_idx = None
                                for i, col_name in enumerate(header):
                                    if col_name and "jill and clients comments" in col_name.lower():
                                        comments_col_idx = i + 1
                                        break

                                if comments_col_idx:
                                    existing_resp = sheets.spreadsheets().values().get(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}"
                                    ).execute()
                                    existing_comment = ""
                                    if existing_resp.get("values"):
                                        existing_comment = existing_resp["values"][0][0] if existing_resp["values"][0] else ""

                                    final_comment = f"{existing_comment.strip()} | {optout_comment}" if existing_comment.strip() else optout_comment

                                    sheets.spreadsheets().values().update(
                                        spreadsheetId=sheet_id,
                                        range=f"{tab_title}!{chr(64 + comments_col_idx)}{new_rownum}",
                                        valueInputOption="RAW",
                                        body={"values": [[final_comment]]}
                                    ).execute()

                                format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                                old_row_became_nonviable = True
                                rownum = new_rownum
                                print(f"🚫 Moved opted-out contact row to NON-VIABLE")
                        except Exception as move_err:
                            print(f"⚠️ Could not move row to NON-VIABLE: {move_err}")

                        # Create notification for user awareness
                        write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=from_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta={
                                "reason": f"contact_optout:{reason}",
                                "details": reason_labels.get(reason, reason),
                                "contact": from_addr_lower,
                                "originalMessage": _full_text[:500]
                            },
                            dedupe_key=f"contact_optout:{thread_id}:{from_addr_lower}"
                        )
                        print(f"🚫 Contact opted out ({reason}): {from_addr_lower}")

                        # Skip auto-response - don't email someone who asked not to be contacted
                        proposal["skip_response"] = True

                    except Exception as e:
                        print(f"❌ Failed to handle contact_optout: {e}")

                elif event_type == "wrong_contact":
                    # This isn't the right person to contact
                    try:
                        reason = event.get("reason", "wrong_person")
                        suggested_contact = event.get("suggestedContact", "")
                        suggested_email = event.get("suggestedEmail", "")
                        suggested_phone = event.get("suggestedPhone", "")

                        reason_labels = {
                            "no_longer_handles": "Contact no longer handles this property",
                            "wrong_person": "Wrong contact for this property",
                            "forwarded": "Message being forwarded to correct person",
                            "left_company": "Contact no longer with company"
                        }

                        # Build details string
                        details = reason_labels.get(reason, reason)
                        if suggested_contact:
                            details += f". Suggested contact: {suggested_contact}"
                        if suggested_email:
                            details += f" ({suggested_email})"
                        if suggested_phone:
                            details += f" - {suggested_phone}"

                        # Create actionable notification
                        write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=from_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta={
                                "reason": f"wrong_contact:{reason}",
                                "details": details,
                                "originalContact": from_addr_lower,
                                "suggestedContact": suggested_contact,
                                "suggestedEmail": suggested_email,
                                "suggestedPhone": suggested_phone,
                                "originalMessage": _full_text[:500]
                            },
                            dedupe_key=f"wrong_contact:{thread_id}:{suggested_email or suggested_contact or from_addr_lower}"
                        )
                        print(f"👤 Wrong contact detected - redirect to: {suggested_contact or 'unknown'} ({suggested_email or 'no email'})")

                        # Skip auto-response - don't reply to wrong person
                        proposal["skip_response"] = True

                    except Exception as e:
                        print(f"❌ Failed to handle wrong_contact: {e}")

            # Required fields check and remaining questions flow
            # Automatic response logic based on property state
            try:
                response_sent = False
                
                # Check if we should skip response (e.g., phone number provided in call request)
                skip_response = proposal.get("skip_response", False)
                if skip_response:
                    print(f"⏭️ Skipping email response (notification only)")
                    return  # Exit early, notification already created
                
                # Check if call was requested but no phone number provided
                call_requested_no_phone = False
                for event in events:
                    if event.get("type") == "call_requested":
                        phone_pattern = r'(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})'
                        phone_match = re.search(phone_pattern, _full_text)
                        if not phone_match:
                            call_requested_no_phone = True
                            break
                
                # Check if LLM generated a response email
                llm_response_email = proposal.get("response_email")
                
                # Scenario 1: Property became non-viable AND new property was suggested
                if old_row_became_nonviable and new_row_created:
                    # Use LLM-generated response if available, otherwise use template
                    if llm_response_email:
                        response_body = llm_response_email
                        print(f"🤖 Using LLM-generated response for non-viable + new property scenario")
                    else:
                        response_body = """Hi,

Thank you for letting me know that property is no longer available, and thanks for suggesting the alternative property.

I'll review the new property details and get back to you if I have any questions."""
                    
                    sent = send_reply_in_thread(user_id, headers, response_body, msg_id, from_addr_lower, thread_id)
                    if sent:
                        print(f"📧 Sent thank you + closing (new property suggested) to: {from_addr_lower}")
                        response_sent = True
                    else:
                        print(f"❌ Failed to send thank you email")
                
                # Scenario 2: Property became non-viable but NO new property suggested
                elif old_row_became_nonviable and not new_row_created:
                    # Use LLM-generated response if available, otherwise use template
                    if llm_response_email:
                        response_body = llm_response_email
                        print(f"🤖 Using LLM-generated response for non-viable scenario")
                    else:
                        response_body = """Hi,

Thank you for letting me know that property is no longer available.

Do you have any other properties that might be a good fit for our requirements?"""
                    
                    sent = send_reply_in_thread(user_id, headers, response_body, msg_id, from_addr_lower, thread_id)
                    if sent:
                        print(f"📧 Sent thank you + ask for alternatives to: {from_addr_lower}")
                        response_sent = True
                    else:
                        print(f"❌ Failed to send alternatives request")
                
                # Handle call request without phone number - send brief response asking for number
                if call_requested_no_phone and not response_sent:
                    response_body = """Hi,

Could you please provide your phone number so I can give you a call?"""
                    sent = send_reply_in_thread(user_id, headers, response_body, msg_id, from_addr_lower, thread_id)
                    if sent:
                        print(f"📞 Sent request for phone number to: {from_addr_lower}")
                        response_sent = True
                    else:
                        print(f"❌ Failed to send phone number request")
                
                # Scenario 3 & 4: Property is still viable - check missing fields
                if not response_sent and not old_row_became_nonviable:
                    sheets = _sheets_client()
                    tab_title = _get_first_tab_title(sheets, sheet_id)
                    
                    # Check if row is below NON-VIABLE divider
                    try:
                        div_resp = sheets.spreadsheets().values().get(
                            spreadsheetId=sheet_id, range=f"{tab_title}!A:A"
                        ).execute()
                        a_col = div_resp.get("values", [])
                        divider_row = None
                        for i, r in enumerate(a_col, start=1):
                            if r and str(r[0]).strip().upper() == "NON-VIABLE":
                                divider_row = i
                                break
                    except Exception as _e:
                        divider_row = None
                    
                    # Skip if row is below divider or if new row was created
                    if new_row_created or (divider_row and rownum > divider_row):
                        print("ℹ️ Skipping response for non-viable or pending new property row")
                    else:
                        # Re-read row data to check missing fields
                        resp = sheets.spreadsheets().values().get(
                            spreadsheetId=sheet_id,
                            range=f"{tab_title}!{rownum}:{rownum}"
                        ).execute()
                        current_row = resp.get("values", [[]])[0] if resp.get("values") else []
                        if len(current_row) < len(header):
                            current_row.extend([""] * (len(header) - len(current_row)))
                        
                        missing_fields = check_missing_required_fields(current_row, header)
                        
                        # CRITICAL: Filter out "Rent/SF /Yr" - it should NEVER be requested
                        missing_fields = [f for f in missing_fields if f != "Rent/SF /Yr"]
                        
                        if missing_fields:
                            # Scenario 3: Thank you + request missing fields
                            # Use LLM-generated response if available, otherwise use template
                            if llm_response_email:
                                response_body = llm_response_email
                                # Safety check: Remove any mention of "Rent/SF /Yr" from LLM response
                                if "Rent/SF /Yr" in response_body or "Rent/SF/Yr" in response_body:
                                    print(f"   ⚠️ LLM response contained 'Rent/SF /Yr', removing it...")
                                    response_body = response_body.replace("Rent/SF /Yr", "").replace("Rent/SF/Yr", "")
                                    # Clean up any double newlines or formatting issues
                                    response_body = "\n".join(line for line in response_body.split("\n") if line.strip() and "Rent/SF" not in line)
                                # Safety check: Remove "Looking forward to your response" phrases
                                if "Looking forward to your response" in response_body or "Looking forward to hearing from you" in response_body:
                                    print(f"   ⚠️ LLM response contained 'Looking forward' phrase, removing it...")
                                    response_body = response_body.replace("Looking forward to your response", "").replace("Looking forward to hearing from you", "")
                                    # Clean up any double newlines
                                    response_body = "\n".join(line for line in response_body.split("\n") if line.strip())
                                    # Ensure it ends with a simple closing if needed
                                    if response_body.strip() and not response_body.strip().endswith("Thanks") and not response_body.strip().endswith("Thanks."):
                                        response_body = response_body.strip() + "\n\nThanks."
                                print(f"🤖 Using LLM-generated response for missing fields scenario")
                            else:
                                field_list = "\n".join(f"- {field}" for field in missing_fields)
                                response_body = f"""Hi,

Thank you for the information!

To complete the property details, could you please provide:

{field_list}"""
                            
                            sent = send_reply_in_thread(user_id, headers, response_body, msg_id, from_addr_lower, thread_id)
                            if sent:
                                print(f"📧 Sent thank you + missing fields request to: {from_addr_lower}")
                            else:
                                print(f"❌ Failed to send missing fields request")
                        else:
                            # Scenario 4: All fields complete - send closing
                            # Use LLM-generated response if available, otherwise use template
                            if llm_response_email:
                                response_body = llm_response_email
                                print(f"🤖 Using LLM-generated response for all fields complete scenario")
                            else:
                                response_body = """Hi,

Thank you for providing all the requested information! We now have everything we need for your property details.

We'll be in touch if we need any additional information."""
                            
                            sent = send_reply_in_thread(user_id, headers, response_body, msg_id, from_addr_lower, thread_id)
                            if sent:
                                print(f"📧 Sent closing email - all fields complete to: {from_addr_lower}")
                            else:
                                print(f"❌ Failed to send closing email")
                        
            except Exception as e:
                print(f"❌ Failed to send automatic response: {e}")
        
        else:
            print("ℹ️ No proposal generated; nothing to apply.")

def scan_inbox_against_index(user_id: str, headers: Dict[str, str], only_unread: bool = True, top: int = 50):
    """
    Idempotent scan of inbox for replies with early exit on processed messages.

    BATCHING: Groups multiple unprocessed messages in the same thread together
    to prevent conflicting auto-responses when contact sends multiple emails quickly.
    """
    base = "https://graph.microsoft.com/v1.0"

    # Calculate 5-hour cutoff
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()  # ends with +00:00

    cutoff_time = now_utc - timedelta(hours=INBOX_SCAN_WINDOW_HOURS)
    cutoff_iso = cutoff_time.isoformat().replace("+00:00", "Z")

    # Build filter with time window
    filters = [f"receivedDateTime ge {cutoff_iso}"]
    if only_unread:
        filters.append("isRead eq false")

    filter_str = " and ".join(filters)

    params = {
        "$top": str(top),
        "$orderby": "receivedDateTime asc",  # CHANGED: oldest first for proper batching
        "$select": "id,subject,from,toRecipients,receivedDateTime,sentDateTime,conversationId,internetMessageId,internetMessageHeaders,bodyPreview",
        "$filter": filter_str
    }

    # PHASE 1: Collect all unprocessed messages and group by thread
    from collections import defaultdict
    thread_messages = defaultdict(list)  # thread_id -> [messages in order]
    orphan_messages = []  # Messages we couldn't match to a thread

    scanned_count = 0
    skipped_count = 0

    try:
        url = f"{base}/me/mailFolders/Inbox/messages"

        while url:
            response = exponential_backoff_request(
                lambda: requests.get(url, headers=headers, params=params, timeout=30)
            )
            data = response.json()
            messages = data.get("value", [])

            if not messages:
                break

            if scanned_count == 0:  # First batch
                print(f"📥 Found {len(messages)} inbox messages to scan")

            for msg in messages:
                scanned_count += 1

                # Check if message is older than scan window
                received_dt = msg.get("receivedDateTime")
                if received_dt:
                    try:
                        msg_time = datetime.fromisoformat(received_dt.replace('Z', '+00:00'))
                        if msg_time < cutoff_time:
                            continue  # Skip but don't stop - we're going oldest first
                    except Exception as e:
                        print(f"⚠️ Failed to parse message time {received_dt}: {e}")

                # Determine processed key (internetMessageId or id)
                processed_key = msg.get("internetMessageId") or msg.get("id")
                if not processed_key:
                    print(f"⚠️ Message has no internetMessageId or id, skipping")
                    continue

                # Check if already processed
                if has_processed(user_id, processed_key):
                    skipped_count += 1
                    continue

                # Try to match to a thread
                thread_id = _match_message_to_thread(user_id, msg, headers)

                if thread_id:
                    thread_messages[thread_id].append(msg)
                else:
                    orphan_messages.append(msg)

            # Handle pagination
            url = data.get("@odata.nextLink")
            if url:
                params = {}  # nextLink includes all parameters

    except Exception as e:
        print(f"❌ Failed to scan inbox: {e}")
        return

    # PHASE 2: Process messages - batched by thread
    processed_count = 0
    batched_count = 0

    # Process thread batches (multiple messages in same thread)
    for thread_id, messages in thread_messages.items():
        if len(messages) > 1:
            # BATCH PROCESSING: Multiple messages in same thread
            print(f"📦 Batching {len(messages)} messages for thread {thread_id[:20]}...")
            batched_count += len(messages) - 1  # Count the extras

            # Process only the LAST message (most recent), but include all message content
            # in the conversation history (which is already handled by build_conversation_payload)
            # First, save all the messages to Firestore so they appear in conversation
            for msg in messages[:-1]:  # All but the last
                try:
                    _save_message_to_thread(user_id, thread_id, msg, headers)
                    processed_key = msg.get("internetMessageId") or msg.get("id")
                    mark_processed(user_id, processed_key)
                except Exception as e:
                    print(f"⚠️ Failed to save batched message: {e}")

            # Process the last message (which will see all previous in conversation)
            last_msg = messages[-1]
            try:
                process_inbox_message(user_id, headers, last_msg)
                processed_count += 1
            except Exception as e:
                print(f"❌ Failed to process batched message: {e}")
            finally:
                processed_key = last_msg.get("internetMessageId") or last_msg.get("id")
                mark_processed(user_id, processed_key)
        else:
            # Single message - process normally
            msg = messages[0]
            try:
                process_inbox_message(user_id, headers, msg)
                processed_count += 1
            except Exception as e:
                print(f"❌ Failed to process message {msg.get('id', 'unknown')}: {e}")
            finally:
                processed_key = msg.get("internetMessageId") or msg.get("id")
                mark_processed(user_id, processed_key)

    # Process orphan messages (couldn't match to thread - will be ignored by process_inbox_message)
    for msg in orphan_messages:
        try:
            process_inbox_message(user_id, headers, msg)
        except Exception as e:
            print(f"❌ Failed to process orphan message: {e}")
        finally:
            processed_key = msg.get("internetMessageId") or msg.get("id")
            mark_processed(user_id, processed_key)

    # Set last scan timestamp
    set_last_scan_iso(user_id, now_utc.isoformat().replace("+00:00", "Z"))

    # Summary log
    if batched_count > 0:
        print(f"📥 Scanned {scanned_count}; processed {processed_count}; batched {batched_count} extra messages; skipped {skipped_count}")
    else:
        print(f"📥 Scanned {scanned_count}; processed {processed_count}; skipped {skipped_count}")


def _match_message_to_thread(user_id: str, msg: dict, headers: dict) -> str | None:
    """
    Try to match an inbox message to an existing thread.
    Returns thread_id if found, None otherwise.
    """
    # Get headers if not present
    internet_message_headers = msg.get("internetMessageHeaders")
    if not internet_message_headers:
        try:
            response = exponential_backoff_request(
                lambda: requests.get(
                    f"https://graph.microsoft.com/v1.0/me/messages/{msg.get('id')}",
                    headers=headers,
                    params={"$select": "internetMessageHeaders"},
                    timeout=30
                )
            )
            internet_message_headers = response.json().get("internetMessageHeaders", [])
        except Exception:
            internet_message_headers = []

    # Extract reply headers
    in_reply_to = None
    references = []

    for header in internet_message_headers or []:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "in-reply-to":
            in_reply_to = normalize_message_id(value)
        elif name == "references":
            references = parse_references_header(value)

    conversation_id = msg.get("conversationId")

    # Try In-Reply-To first
    if in_reply_to:
        thread_id = lookup_thread_by_message_id(user_id, in_reply_to)
        if thread_id:
            return thread_id

    # Try References (newest to oldest)
    if references:
        for ref in reversed(references):
            ref = normalize_message_id(ref)
            thread_id = lookup_thread_by_message_id(user_id, ref)
            if thread_id:
                return thread_id

    # Fallback to conversation ID
    if conversation_id:
        thread_id = lookup_thread_by_conversation_id(user_id, conversation_id)
        if thread_id:
            return thread_id

    return None


def _save_message_to_thread(user_id: str, thread_id: str, msg: dict, headers: dict):
    """
    Save a message to a thread without full processing.
    Used for batching - saves earlier messages so they appear in conversation history.
    """
    from_info = msg.get("from", {}).get("emailAddress", {})
    from_addr = from_info.get("address", "")
    internet_message_id = msg.get("internetMessageId")
    received_dt = msg.get("receivedDateTime")
    sent_dt = msg.get("sentDateTime")
    subject = msg.get("subject", "")
    to_recipients = [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])]

    # Fetch full body
    try:
        full_body_resp = exponential_backoff_request(
            lambda: requests.get(
                f"https://graph.microsoft.com/v1.0/me/messages/{msg.get('id')}",
                headers=headers,
                params={"$select": "body"},
                timeout=30
            )
        ).json().get("body", {}) or {}
        _raw_content = full_body_resp.get("content", "") or ""
        _ctype = (full_body_resp.get("contentType") or "Text").upper()
        _full_text = strip_html_tags(_raw_content) if _ctype == "HTML" else _raw_content
    except Exception:
        _full_text = msg.get("bodyPreview", "")

    # Get headers for in_reply_to and references
    internet_message_headers = msg.get("internetMessageHeaders", [])
    in_reply_to = None
    references = []

    for header in internet_message_headers or []:
        name = header.get("name", "").lower()
        value = header.get("value", "")
        if name == "in-reply-to":
            in_reply_to = normalize_message_id(value)
        elif name == "references":
            references = parse_references_header(value)

    # Create message record
    message_record = {
        "direction": "inbound",
        "subject": subject,
        "from": from_addr,
        "to": to_recipients,
        "sentDateTime": sent_dt,
        "receivedDateTime": received_dt,
        "headers": {
            "internetMessageId": internet_message_id,
            "inReplyTo": in_reply_to,
            "references": references
        },
        "body": {
            "contentType": "Text",
            "content": _full_text,
            "preview": safe_preview(_full_text)
        }
    }

    # Save to Firestore
    if internet_message_id:
        save_message(user_id, thread_id, internet_message_id, message_record)
        index_message_id(user_id, internet_message_id, thread_id)

    # Update thread timestamp
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(thread_id)
        thread_ref.set({"updatedAt": SERVER_TIMESTAMP}, merge=True)
    except Exception:
        pass

    print(f"  📝 Saved batched message from {from_addr} to thread {thread_id[:20]}...")

def scan_sent_items_for_manual_replies(user_id: str, headers: Dict[str, str], top: int = 50):
    """
    Scan SentItems for Jill's manual replies to conversations we're tracking.
    Indexes them so they appear in conversation history.
    """
    try:
        from .utils import exponential_backoff_request, safe_preview, strip_html_tags
        from .messaging import save_message, index_message_id, index_conversation_id, lookup_thread_by_conversation_id, save_thread_root
        from datetime import datetime, timezone, timedelta
        import requests
        
        base = "https://graph.microsoft.com/v1.0"
        
        # Calculate 5-hour cutoff
        now_utc = datetime.now(timezone.utc)
        cutoff_time = now_utc - timedelta(hours=INBOX_SCAN_WINDOW_HOURS)
        cutoff_iso = cutoff_time.isoformat().replace("+00:00", "Z")
        
        # Get all tracked conversation IDs from Firestore
        threads_ref = _fs.collection("users").document(user_id).collection("threads")
        threads = list(threads_ref.stream())
        tracked_conversation_ids = set()
        
        for thread_doc in threads:
            thread_data = thread_doc.to_dict() or {}
            conv_id = thread_data.get("conversationId")
            if conv_id:
                tracked_conversation_ids.add(conv_id)
        
        if not tracked_conversation_ids:
            print("📭 No tracked conversations found, skipping SentItems scan")
            return
        
        print(f"📤 Scanning SentItems for manual replies in {len(tracked_conversation_ids)} tracked conversations...")
        
        # Scan SentItems for messages in tracked conversations
        params = {
            "$top": str(top),
            "$orderby": "sentDateTime desc",
            "$select": "id,subject,from,toRecipients,sentDateTime,conversationId,internetMessageId,body,bodyPreview",
            "$filter": f"sentDateTime ge {cutoff_iso}"
        }
        
        processed_count = 0
        scanned_count = 0
        
        try:
            url = f"{base}/me/mailFolders/SentItems/messages"
            
            while url:
                response = exponential_backoff_request(
                    lambda: requests.get(url, headers=headers, params=params, timeout=30)
                )
                data = response.json()
                messages = data.get("value", [])
                
                if not messages:
                    break
                
                for msg in messages:
                    scanned_count += 1
                    
                    # Check if message is older than 5 hours
                    sent_dt = msg.get("sentDateTime")
                    if sent_dt:
                        try:
                            msg_time = datetime.fromisoformat(sent_dt.replace('Z', '+00:00'))
                            if msg_time < cutoff_time:
                                url = None  # Stop pagination
                                break
                        except Exception as e:
                            print(f"⚠️ Failed to parse message time {sent_dt}: {e}")
                    
                    conversation_id = msg.get("conversationId")
                    if not conversation_id or conversation_id not in tracked_conversation_ids:
                        continue  # Not in a tracked conversation
                    
                    internet_message_id = msg.get("internetMessageId")
                    if not internet_message_id:
                        continue  # Need message ID to index
                    
                    # Check if already indexed
                    normalized_id = normalize_message_id(internet_message_id)
                    from .messaging import lookup_thread_by_message_id
                    existing_thread = lookup_thread_by_message_id(user_id, internet_message_id)
                    
                    if existing_thread:
                        continue  # Already indexed
                    
                    # Find or create thread for this conversation
                    thread_id = lookup_thread_by_conversation_id(user_id, conversation_id)
                    
                    if not thread_id:
                        # Create new thread from conversation
                        thread_id = normalize_message_id(conversation_id) or conversation_id
                        thread_meta = {
                            "subject": msg.get("subject", "Property information"),
                            "email": [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])],
                            "conversationId": conversation_id,
                            "createdFromSentItem": True
                        }
                        # Save thread with retry
                        for attempt in range(3):
                            if save_thread_root(user_id, thread_id, thread_meta):
                                break
                            time.sleep(0.5 * (attempt + 1))
                        # Index conversation with retry
                        for attempt in range(3):
                            if index_conversation_id(user_id, conversation_id, thread_id):
                                break
                            time.sleep(0.5 * (attempt + 1))
                        print(f"   📝 Created new thread from SentItem: {thread_id}")
                    
                    # Index this sent message
                    to_recipients = [r.get("emailAddress", {}).get("address", "") for r in msg.get("toRecipients", [])]
                    body_obj = msg.get("body", {}) or {}
                    body_content = body_obj.get("content", "")
                    body_type = body_obj.get("contentType", "Text")
                    if body_type == "HTML":
                        body_content = strip_html_tags(body_content)
                    
                    message_record = {
                        "direction": "outbound",
                        "subject": msg.get("subject", ""),
                        "from": "me",
                        "to": to_recipients,
                        "sentDateTime": sent_dt,
                        "receivedDateTime": None,
                        "headers": {
                            "internetMessageId": internet_message_id,
                            "inReplyTo": None,
                            "references": []
                        },
                        "body": {
                            "contentType": body_type,
                            "content": body_content,
                            "preview": msg.get("bodyPreview", "")[:200] or safe_preview(body_content)
                        }
                    }
                    
                    # Save message with retry
                    for attempt in range(3):
                        if save_message(user_id, thread_id, normalized_id, message_record):
                            break
                        time.sleep(0.5 * (attempt + 1))

                    # Index message with retry and verification
                    msg_indexed = False
                    for attempt in range(3):
                        if index_message_id(user_id, internet_message_id, thread_id):
                            time.sleep(0.2)
                            if lookup_thread_by_message_id(user_id, internet_message_id) == thread_id:
                                msg_indexed = True
                                break
                        time.sleep(0.5 * (attempt + 1))

                    if not msg_indexed:
                        print(f"   ⚠️ Failed to index manual reply after retries")

                    processed_count += 1
                    print(f"   📝 Indexed manual reply: {internet_message_id[:50]}... -> thread {thread_id}")
                
                # Check for next page
                url = data.get("@odata.nextLink")
                if url:
                    params = None  # NextLink includes all params
                else:
                    url = None
            
            if processed_count > 0:
                print(f"📤 Indexed {processed_count} manual reply(s) from SentItems")
            else:
                print(f"📤 No new manual replies found in SentItems")
                
        except Exception as e:
            print(f"❌ Failed to scan SentItems: {e}")
            
    except Exception as e:
        print(f"❌ Failed to scan SentItems for manual replies: {e}")