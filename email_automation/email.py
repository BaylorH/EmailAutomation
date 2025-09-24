import json
import requests
from typing import Dict, List, Optional
from datetime import datetime, timezone
from .utils import exponential_backoff_request, safe_preview, _body_kind
from .messaging import save_thread_root, save_message, index_message_id, index_conversation_id
from .clients import _get_sheet_id_or_fail, _sheets_client
from .sheets import _find_row_by_email, _get_first_tab_title, _read_header_row2, _header_index_map
from .utils import normalize_message_id

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
    """Send email and immediately index it in Firestore for reply tracking."""
    if not recipients:
        return {"sent": [], "errors": {"_all": "No recipients"}}

    content_type, content = _body_kind(script)
    results = {"sent": [], "errors": {}}
    base = "https://graph.microsoft.com/v1.0"

    for addr in recipients:
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

            # 4. Index in Firestore
            root_id = normalize_message_id(internet_message_id)
            
            # Thread root
            thread_meta = {
                "subject": subject,
                "clientId": client_id_or_none,
                "email": [addr],
                "conversationId": conversation_id,
            }
            
            # NEW: Store row number for anchoring if provided
            if row_number:
                thread_meta["rowNumber"] = row_number
            
            save_thread_root(user_id, root_id, thread_meta)
            
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
            save_message(user_id, root_id, root_id, message_record)
            
            # Index message
            index_message_id(user_id, internet_message_id, root_id)
            
            # Index conversation (optional fallback)
            if conversation_id:
                index_conversation_id(user_id, conversation_id, root_id)

            results["sent"].append(addr)
            print(f"✅ Sent and indexed email to {addr} (threadId: {root_id})")
            
        except Exception as e:
            msg = str(e)
            print(f"❌ Failed to send/index to {addr}: {msg}")
            results["errors"][addr] = msg

    return results

def send_outboxes(user_id: str, headers):
    """Modified to use send_and_index_email instead of send_email."""
    from .clients import _fs
    
    outbox_ref = _fs.collection("users").document(user_id).collection("outbox")
    docs = list(outbox_ref.stream())

    if not docs:
        print("📭 Outbox empty")
        return

    print(f"📬 Found {len(docs)} outbox item(s)")
    for d in docs:
        data = d.to_dict() or {}
        emails = data.get("assignedEmails") or []
        script = data.get("script") or ""
        clientId = (data.get("clientId") or "").strip()

        print(f"→ Sending outbox item {d.id} to {len(emails)} recipient(s) (clientId={clientId or 'n/a'})")

        try:
            # Use new send_and_index_email function
            res = send_and_index_email(user_id, headers, script, emails, client_id_or_none=clientId)
            any_errors = bool(res["errors"])

            if not any_errors and res["sent"]:
                d.reference.delete()
                print(f"🗑️ Deleted outbox item {d.id}")
            else:
                attempts = int(data.get("attempts") or 0) + 1
                d.reference.set(
                    {"attempts": attempts, "lastError": json.dumps(res["errors"])[:1500]},
                    merge=True,
                )
                print(f"⚠️ Kept item {d.id} with error; attempts={attempts}")

        except Exception as e:
            attempts = int(data.get("attempts") or 0) + 1
            d.reference.set(
                {"attempts": attempts, "lastError": str(e)[:1500]},
                merge=True,
            )
            print(f"💥 Error sending item {d.id}: {e}; attempts={attempts}")

# Legacy Functions (kept for compatibility)
def send_email(headers, script: str, emails: list[str], client_id: str | None = None):
    """Legacy function - redirects to send_and_index_email"""
    # Note: This legacy function doesn't have user_id, so it can't use the new pipeline
    # Users should migrate to send_and_index_email directly
    raise NotImplementedError("send_email is deprecated. Use send_and_index_email with user_id parameter.")