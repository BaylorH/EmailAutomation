import re
import requests
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter

from .clients import _fs, _get_sheet_id_or_fail, _sheets_client
from .sheets import format_sheet_columns_autosize_with_exceptions, _get_first_tab_title, _read_header_row2, append_links_to_flyer_link_column, _header_index_map
from .sheet_operations import _find_row_by_anchor, ensure_nonviable_divider, move_row_below_divider, insert_property_row_above_divider, _is_row_below_nonviable
from .messaging import (save_message, index_message_id, dump_thread_from_firestore, 
                       has_processed, mark_processed, set_last_scan_iso, 
                       lookup_thread_by_message_id, lookup_thread_by_conversation_id)
from .logging import write_message_order_test
from .ai_processing import propose_sheet_updates, apply_proposal_to_sheet, get_row_anchor, check_missing_required_fields
from .file_handling import fetch_pdf_attachments, upload_pdf_to_drive, upload_pdf_user_data
from .notifications import write_notification, add_client_notifications
from .utils import (exponential_backoff_request, strip_html_tags, safe_preview, 
                   parse_references_header, normalize_message_id, fetch_url_as_text, _sanitize_url)
from .email_operations import send_remaining_questions_email, send_closing_email
from .app_config import REQUIRED_FIELDS_FOR_CLOSE

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
    from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "")
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
    
    if not thread_id:
        print(f"❓ No thread match found for message from {from_addr}")
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
    
    # Save to Firestore
    if internet_message_id:
        save_message(user_id, thread_id, internet_message_id, message_record)
        index_message_id(user_id, internet_message_id, thread_id)
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
    
    # Only proceed if we successfully matched a sheet row
    if sheet_id and rownum is not None:
        from_addr_lower = (from_addr or "").lower()

        # --- flags for gating later ---
        old_row_became_nonviable = False   # set true when we move the row below divider
        new_row_created = False            # set true when we insert a new property row
        new_row_number = None              # track the newly created row number

        # NEW: Handle PDF attachments for current message only
        file_ids_for_this_run = []
        file_manifest = []
        pdf_attachments = fetch_pdf_attachments(headers, msg_id)
        
        if pdf_attachments:
            drive_links = []
            
            for pdf in pdf_attachments:
                try:
                    # Upload to Drive
                    drive_link = upload_pdf_to_drive(pdf["name"], pdf["bytes"])
                    if drive_link:
                        drive_links.append(drive_link)
                    
                    # Upload to OpenAI
                    file_id = upload_pdf_user_data(pdf["name"], pdf["bytes"])
                    file_ids_for_this_run.append(file_id)
                    file_manifest.append({"id": file_id, "name": pdf["name"]})
                    
                except Exception as e:
                    print(f"❌ Failed to process PDF {pdf['name']}: {e}")
            
            # Append Drive links to Flyer / Link column on the current row (keep existing behavior)
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
        
        # Step 3: get proposal using Responses API with URL content
        proposal = propose_sheet_updates(
            user_id, client_id, from_addr_lower, sheet_id, header, rownum, rowvals, 
            thread_id, file_manifest=file_manifest, url_texts=url_texts
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

                    now_id = datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-").replace("+00:00", "Z")
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
                    # Create action_needed notification
                    try:
                        write_notification(
                            user_id, client_id,
                            kind="action_needed",
                            priority="important",
                            email=from_addr_lower,
                            thread_id=thread_id,
                            row_number=rownum,
                            row_anchor=row_anchor,
                            meta={"reason": "call_requested", "details": "Call requested in conversation"},
                            dedupe_key=f"call_requested:{thread_id}"
                        )
                        print(f"📞 Created call_requested notification")
                    except Exception as e:
                        print(f"❌ Failed to write call_requested notification: {e}")
                
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
                            divider_row = ensure_nonviable_divider(sheets, sheet_id, tab_title)
                            new_rownum = move_row_below_divider(sheets, sheet_id, tab_title, rownum, divider_row)
                            
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

            # Required fields check and remaining questions flow
            try:
                sheets = _sheets_client()
                tab_title = _get_first_tab_title(sheets, sheet_id)

                # Stateless divider check so we don't nag on non-viable rows
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

                if old_row_became_nonviable or new_row_created or (divider_row and rownum > divider_row):
                    print("ℹ️ Skipping remaining-questions for old row (non-viable and/or new property pending).")
                else:
                    # Re-read row data in case it was updated
                    resp = sheets.spreadsheets().values().get(
                        spreadsheetId=sheet_id,
                        range=f"{tab_title}!{rownum}:{rownum}"
                    ).execute()
                    current_row = resp.get("values", [[]])[0] if resp.get("values") else []
                    if len(current_row) < len(header):
                        current_row.extend([""] * (len(header) - len(current_row)))
                    
                    missing_fields = check_missing_required_fields(current_row, header)
                    
                    if missing_fields:
                        # Send remaining questions email
                        sent = send_remaining_questions_email(
                            user_id, client_id, headers, from_addr_lower, 
                            missing_fields, thread_id, rownum, row_anchor
                        )
                        if sent:
                            print(f"📧 Sent remaining questions for {len(missing_fields)} missing fields")
                    else:
                        # All required fields complete - send closing email
                        sent = send_closing_email(
                            user_id, client_id, headers, from_addr_lower, 
                            thread_id, rownum, row_anchor
                        )
                        if sent:
                            print(f"🎉 Sent closing email - all required fields complete")
                        
            except Exception as e:
                print(f"❌ Failed to send remaining questions email: {e}")
        
        else:
            print("ℹ️ No proposal generated; nothing to apply.")

def scan_inbox_against_index(user_id: str, headers: Dict[str, str], only_unread: bool = True, top: int = 50):
    """Idempotent scan of inbox for replies with early exit on processed messages."""
    base = "https://graph.microsoft.com/v1.0"
    
    # Calculate 5-hour cutoff
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()  # ends with +00:00

    cutoff_time = now_utc - timedelta(hours=5)
    cutoff_iso = cutoff_time.isoformat().replace("+00:00", "Z")
    
    # Build filter with time window
    filters = [f"receivedDateTime ge {cutoff_iso}"]
    if only_unread:
        filters.append("isRead eq false")
    
    filter_str = " and ".join(filters)
    
    params = {
        "$top": str(top),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,toRecipients,receivedDateTime,sentDateTime,conversationId,internetMessageId,internetMessageHeaders,bodyPreview",
        "$filter": filter_str
    }

    processed_count = 0
    scanned_count = 0
    skipped_count = 0
    hit_known = False
    peek_counter = 0
    
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
                print(f"📥 Found {len(messages)} inbox messages to process")
            
            for msg in messages:
                scanned_count += 1
                
                # Check if message is older than 5 hours
                received_dt = msg.get("receivedDateTime")
                if received_dt:
                    try:
                        msg_time = datetime.fromisoformat(received_dt.replace('Z', '+00:00'))
                        if msg_time < cutoff_time:
                            print(f"⏰ Message older than 5 hours, stopping scan")
                            url = None  # Stop pagination
                            break
                    except Exception as e:
                        print(f"⚠️ Failed to parse message time {received_dt}: {e}")
                
                # Determine processed key (internetMessageId or id)
                processed_key = msg.get("internetMessageId") or msg.get("id")
                if not processed_key:
                    print(f"⚠️ Message has no internetMessageId or id, skipping")
                    continue
                
                # Check if already processed
                if has_processed(user_id, processed_key):
                    if not hit_known:
                        hit_known = True
                        print(f"⛳ Hit already-processed message; peeking 3 more and stopping")
                    
                    # Count this as skipped and increment peek counter
                    skipped_count += 1
                    peek_counter += 1
                    
                    # Stop after peeking 3 more
                    if peek_counter >= 3:
                        url = None  # Stop pagination
                        break
                    continue
                
                # If we're in peek mode but this message isn't processed, still process it
                # but continue counting down the peek
                if hit_known:
                    peek_counter += 1
                
                # Process the message
                try:
                    process_inbox_message(user_id, headers, msg)
                    processed_count += 1
                except Exception as e:
                    print(f"❌ Failed to process message {msg.get('id', 'unknown')}: {e}")
                finally:
                    # Always mark as processed to avoid reprocessing
                    mark_processed(user_id, processed_key)
                
                # Stop after peeking if we hit known messages
                if hit_known and peek_counter >= 3:
                    url = None  # Stop pagination
                    break
            
            # Handle pagination - but stop if we hit processed messages and finished peeking
            if url and not hit_known:
                url = data.get("@odata.nextLink")
                params = {}  # nextLink includes all parameters
            else:
                url = None
                
    except Exception as e:
        print(f"❌ Failed to scan inbox: {e}")
        return
    
    # Set last scan timestamp
    set_last_scan_iso(user_id, now_utc.isoformat().replace("+00:00", "Z"))
    
    # Summary log
    print(f"📥 Scanned {scanned_count} message(s); processed {processed_count}; skipped {skipped_count}")