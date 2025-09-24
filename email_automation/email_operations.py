import hashlib
import requests
from typing import List, Dict
from google.cloud.firestore import FieldFilter
from .clients import _fs
from .notifications import write_notification
from .utils import exponential_backoff_request
from .app_config import REQUIRED_FIELDS_FOR_CLOSE

def send_remaining_questions_email(uid: str, client_id: str, headers: dict, recipient: str, 
                                 missing_fields: list[str], thread_id: str, row_number: int,
                                 row_anchor: str) -> bool:
    """
    Send a remaining questions email in the same thread (idempotent).
    Returns True if sent, False if skipped (duplicate).
    """
    try:
        # Create content hash for idempotency
        content_key = f"missing:{','.join(sorted(missing_fields))}"
        content_hash = hashlib.sha256(content_key.encode('utf-8')).hexdigest()[:16]
        
        # Check if we already sent this exact list
        dedupe_key = f"remaining_questions:{thread_id}:{content_hash}"
        
        # Simple check: look for recent similar notifications
        recent_notifs_query = (_fs.collection("users").document(uid)
                              .collection("clients").document(client_id)
                              .collection("notifications")
                              .where(filter=FieldFilter("threadId", "==", thread_id))
                              .where(filter=FieldFilter("kind", "==", "action_needed"))
                              .limit(5))
        
        # Execute the query and iterate through results
        for notif in recent_notifs_query.stream():
            notif_data = notif.to_dict()
            if notif_data and notif_data.get("dedupeKey") == dedupe_key:
                print(f"📧 Skipped duplicate remaining questions email")
                return False
        
        # Compose email
        field_list = "\n".join(f"- {field}" for field in missing_fields)
        
        body = f"""Hi,

We still need the following information to complete your property details:

{field_list}

Could you please provide these details when you have a moment?

Thanks!"""
        
        base = "https://graph.microsoft.com/v1.0"
        # 1) Find Graph message id by our stored internetMessageId (thread_id)
        q = {"$filter": f"internetMessageId eq '{thread_id}'", "$select": "id"}
        lookup = requests.get(f"{base}/me/messages", headers=headers, params=q, timeout=30)
        lookup.raise_for_status()
        vals = lookup.json().get("value", [])

        if vals:
            graph_id = vals[0]["id"]
            # 2) Reply in-thread (this preserves proper headers)
            reply_payload = {"comment": body}
            resp = requests.post(f"{base}/me/messages/{graph_id}/reply",
                                 headers=headers, json=reply_payload, timeout=30)
            resp.raise_for_status()
        else:
            # 3) Fallback: send a new email (no custom In-Reply-To headers)
            msg = {
                "subject": "Remaining questions",
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": recipient}}],
            }
            send_payload = {"message": msg, "saveToSentItems": True}
            resp = requests.post(f"{base}/me/sendMail", headers=headers, json=send_payload, timeout=30)
            resp.raise_for_status()
        
        # Create action_needed notification
        write_notification(
            uid, client_id,
            kind="action_needed",
            priority="important",
            email=recipient,
            thread_id=thread_id,
            row_number=row_number,
            row_anchor=row_anchor,
            meta={"reason": "missing_fields", "details": f"Missing: {', '.join(missing_fields)}"},
            dedupe_key=dedupe_key
        )
        
        print(f"📧 Sent remaining questions email for {len(missing_fields)} missing fields")
        return True
        
    except Exception as e:
        print(f"❌ Failed to send remaining questions email: {e}")
        return False

def send_closing_email(uid: str, client_id: str, headers: dict, recipient: str, 
                      thread_id: str, row_number: int, row_anchor: str) -> bool:
    """Send polite closing email when all required fields are complete."""
    try:
        body = """Hi,

Thank you for providing all the requested information! We now have everything we need for your property details.

We'll be in touch if we need any additional information.

Best regards"""
        
        # Send email using sendMail endpoint
        base = "https://graph.microsoft.com/v1.0"
        msg = {
            "subject": "Re: Property information complete",
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": thread_id},
                {"name": "x-row-anchor", "value": f"rowNumber={row_number}"}
            ]
        }
        
        send_payload = {"message": msg, "saveToSentItems": True}
        response = requests.post(f"{base}/me/sendMail", headers=headers, json=send_payload, timeout=30)
        response.raise_for_status()
        
        # Create row_completed notification
        write_notification(
            uid, client_id,
            kind="row_completed",
            priority="important",
            email=recipient,
            thread_id=thread_id,
            row_number=row_number,
            row_anchor=row_anchor,
            meta={"completedFields": REQUIRED_FIELDS_FOR_CLOSE, "missingFields": []},
            dedupe_key=f"row_completed:{thread_id}:{row_number}"
        )
        
        print(f"📧 Sent closing email for completed row {row_number}")
        return True
        
    except Exception as e:
        print(f"❌ Failed to send closing email: {e}")
        return False

def send_new_property_email(uid: str, client_id: str, headers: dict, recipient: str, 
                          address: str, city: str, row_number: int) -> str | None:
    """
    Send a new thread email for a new property suggestion.
    Returns the new thread ID if successful.
    """
    try:
        subject = f"{address}, {city}" if city else address
        
        body = f"""Hi,

We noticed you mentioned a new property: {address}{', ' + city if city else ''}.

Could you please provide the following details for this property:

- Total square footage
- Rent per square foot per year
- Operating expenses per square foot
- Number of drive-in doors
- Number of dock doors  
- Ceiling height
- Power specifications

Thanks!"""
        
        # Send as new email (not a reply)
        base = "https://graph.microsoft.com/v1.0"
        msg = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": recipient}}],
            "internetMessageHeaders": [
                {"name": "x-client-id", "value": client_id},
                {"name": "x-row-anchor", "value": f"rowNumber={row_number}"}
            ]
        }
        
        # Create draft first to get message ID
        create_response = requests.post(f"{base}/me/messages", headers=headers, json=msg, timeout=30)
        create_response.raise_for_status()
        draft_id = create_response.json()["id"]
        
        # Get message identifiers
        get_response = requests.get(
            f"{base}/me/messages/{draft_id}",
            headers=headers,
            params={"$select": "internetMessageId,conversationId,subject,toRecipients"},
            timeout=30
        )
        get_response.raise_for_status()
        message_data = get_response.json()
        
        internet_message_id = message_data.get("internetMessageId")
        conversation_id = message_data.get("conversationId")
        
        if not internet_message_id:
            raise Exception("No internetMessageId returned from Graph")
        
        # Send draft
        requests.post(f"{base}/me/messages/{draft_id}/send", headers=headers, timeout=30)
        
        # Index in Firestore
        from utils import normalize_message_id
        from messaging import save_thread_root, save_message, index_message_id, index_conversation_id
        from datetime import datetime, timezone
        from utils import safe_preview
        
        root_id = normalize_message_id(internet_message_id)
        
        # Thread root with rowNumber for anchoring
        thread_meta = {
            "subject": subject,
            "clientId": client_id,
            "email": [recipient],
            "conversationId": conversation_id,
            "rowNumber": row_number  # NEW: Store row number for anchoring
        }
        save_thread_root(uid, root_id, thread_meta)
        
        # Message record
        message_record = {
            "direction": "outbound",
            "subject": subject,
            "from": "me",
            "to": [recipient],
            "sentDateTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "receivedDateTime": None,
            "headers": {
                "internetMessageId": internet_message_id,
                "inReplyTo": None,
                "references": []
            },
            "body": {
                "contentType": "Text",
                "content": body,
                "preview": f"New property questions for {address}"
            }
        }
        save_message(uid, root_id, root_id, message_record)
        
        # Index message
        index_message_id(uid, internet_message_id, root_id)
        if conversation_id:
            index_conversation_id(uid, conversation_id, root_id)
        
        print(f"📧 Sent new property email for {address} -> thread {root_id}")
        return root_id
        
    except Exception as e:
        print(f"❌ Failed to send new property email: {e}")
        return None