import os
import json
import atexit
import base64
import requests
from urllib.parse import quote
from openpyxl import Workbook
from msal import ConfidentialClientApplication, SerializableTokenCache

from firebase_helpers import download_token, upload_token, upload_excel

from google.cloud import firestore
from google.cloud.firestore import SERVER_TIMESTAMP

import re
import time
from datetime import datetime
from typing import Optional, List, Dict, Any

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request


# ─── Config ─────────────────────────────────────────────
CLIENT_ID         = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET     = os.getenv("AZURE_API_CLIENT_SECRET")
FIREBASE_API_KEY  = os.getenv("FIREBASE_API_KEY")
FIREBASE_BUCKET   = "email-automation-cache.firebasestorage.app"
AUTHORITY         = "https://login.microsoftonline.com/common"
SCOPES            = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE       = "msal_token_cache.bin"

SUBJECT = "Weekly Questions"
BODY = (
    "Hi,\n\nPlease answer the following:\n"
    "1. How was your week?\n"
    "2. What challenges did you face?\n"
    "3. Any updates to share?\n\nThanks!"
)
THANK_YOU_BODY = "Thanks for your response."

if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
    raise RuntimeError("❌ Missing required env vars")

# Firestore Admin client (uses GOOGLE_APPLICATION_CREDENTIALS)
_fs = firestore.Client()

# ─── Helper: detect HTML vs text ───────────────────────
_html_rx = re.compile(r"<[a-zA-Z/][^>]*>")

def _body_kind(script: str):
    if script and _html_rx.search(script):
        return "HTML", script
    return "Text", script or ""


def _helper_google_creds():
    client_id     = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError("❌ Missing GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    creds.refresh(Request())
    return creds

def _sheets_client():
    creds  = _helper_google_creds()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return sheets


def _get_sheet_id_or_fail(uid: str, client_id: str) -> str:
    # Try active clients
    doc_ref = _fs.collection("users").document(uid).collection("clients").document(client_id).get()
    if doc_ref.exists:
        sid = (doc_ref.to_dict() or {}).get("sheetId")
        if sid:
            return sid

    # Try archived clients (emails might keep flowing after archive)
    doc_ref = _fs.collection("users").document(uid).collection("archivedClients").document(client_id).get()
    if doc_ref.exists:
        sid = (doc_ref.to_dict() or {}).get("sheetId")
        if sid:
            return sid

    # Required by design → fail loudly
    raise RuntimeError(f"❌ sheetId not found for uid={uid} clientId={client_id}. This field is required.")


# --- SHEETS HELPERS (row 2 = header) ---------------------------------

def _get_first_tab_title(sheets, spreadsheet_id: str) -> str:
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return meta["sheets"][0]["properties"]["title"]

def _read_header_row2(sheets, spreadsheet_id: str, tab_title: str) -> list[str]:
    # Entire row 2 regardless of width
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_title}!2:2"
    ).execute()
    vals = resp.get("values", [[]])
    return vals[0] if vals else []

def _normalize_email(s: str) -> str:
    return (s or "").strip().lower()

def _guess_email_col_idx(header: list[str]) -> int:
    candidates = {"email", "email address", "contact email", "e-mail", "e mail"}
    for i, h in enumerate(header):
        if _normalize_email(h) in candidates:
            return i
    return -1

def _find_row_by_email(sheets, spreadsheet_id: str, tab_title: str, header: list[str], email: str):
    """
    Returns (row_number, row_values) where row_number is the 1-based sheet row.
    Header is row 2, data starts at row 3.
    """
    if not email:
        return None, None

    # Pull header + all data rows with a very wide column cap
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_title}!A2:ZZZ"
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        return None, None

    # rows[0] is header (row 2), rows[1:] are data starting row 3
    data_rows = rows[1:]
    email_idx = _guess_email_col_idx(header)
    needle = _normalize_email(email)

    for offset, row in enumerate(data_rows, start=3):  # sheet row numbers
        # pad row to header length so indexing is safe
        padded = row + [""] * (max(0, len(header) - len(row)))

        candidate = None
        if email_idx >= 0:
            candidate = padded[email_idx]
            if _normalize_email(candidate) == needle:
                return offset, padded

        # fallback: scan all cells for an exact email match
        if email_idx < 0:
            for cell in padded:
                if _normalize_email(cell) == needle:
                    return offset, padded

    return None, None


def fetch_and_log_sheet_for_thread(uid: str, thread_id: str, counterparty_email: str | None):
    # Read thread (to get clientId)
    tdoc = (_fs.collection("users").document(uid)
            .collection("threads").document(thread_id).get())
    if not tdoc.exists:
        print("⚠️ Thread doc not found; cannot fetch sheet")
        return

    tdata = tdoc.to_dict() or {}
    client_id = tdata.get("clientId")
    if not client_id:
        print("⚠️ Thread has no clientId; cannot fetch sheet")
        return

    # Required: sheetId on client doc
    sheet_id = _get_sheet_id_or_fail(uid, client_id)

    # Counterparty email fallback: use thread's stored recipients if missing
    if not counterparty_email:
        recips = tdata.get("email") or []
        if recips:
            counterparty_email = recips[0]

    # Connect to Sheets; header = row 2
    sheets = _sheets_client()
    tab_title = _get_first_tab_title(sheets, sheet_id)
    header = _read_header_row2(sheets, sheet_id, tab_title)

    print(f"📄 Sheet fetched: title='{tab_title}', sheetId={sheet_id}")
    print(f"   Header (row 2): {header}")
    print(f"   Counterparty email (row match): {counterparty_email or 'unknown'}")

    # Find the row matching the counterparty email and print it
    rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab_title, header, counterparty_email or "")
    if rownum is not None:
        print(f"📌 Matched row {rownum}: {rowvals}")
    else:
        # Be loud – row must exist for our workflow
        print(f"❌ No sheet row found with email = {counterparty_email}")


def b64url_id(message_id: str) -> str:
    """Encode message ID for safe use as Firestore document key."""
    return base64.urlsafe_b64encode(message_id.encode('utf-8')).decode('ascii').rstrip('=')

def normalize_message_id(msg_id: str) -> str:
    """Normalize message ID - keep as-is but strip whitespace."""
    return msg_id.strip() if msg_id else ""

def parse_references_header(references: str) -> List[str]:
    """Parse References header into list of message IDs."""
    if not references:
        return []
    
    # Split by whitespace and filter non-empty tokens
    tokens = [token.strip() for token in references.split() if token.strip()]
    return tokens

def strip_html_tags(html: str) -> str:
    """Strip HTML tags for preview."""
    if not html:
        return ""
    # Simple HTML tag removal
    clean = re.sub(r'<[^>]+>', '', html)
    # Decode common HTML entities
    clean = clean.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    clean = clean.replace('&quot;', '"').replace('&#39;', "'")
    return clean.strip()

def safe_preview(content: str, max_len: int = 200) -> str:
    """Create safe preview of email content."""
    preview = strip_html_tags(content) if content else ""
    if len(preview) > max_len:
        preview = preview[:max_len] + "..."
    return preview

def save_thread_root(user_id: str, root_id: str, meta: Dict[str, Any]):
    """Save or update thread root document."""
    try:
        thread_ref = _fs.collection("users").document(user_id).collection("threads").document(root_id)
        meta["updatedAt"] = SERVER_TIMESTAMP
        if "createdAt" not in meta:
            meta["createdAt"] = SERVER_TIMESTAMP
        
        thread_ref.set(meta, merge=True)
        print(f"💾 Saved thread root: {root_id}")
    except Exception as e:
        print(f"❌ Failed to save thread root {root_id}: {e}")

def save_message(user_id: str, thread_id: str, message_id: str, payload: Dict[str, Any]):
    """Save message to thread."""
    try:
        msg_ref = (_fs.collection("users").document(user_id)
                   .collection("threads").document(thread_id)
                   .collection("messages").document(message_id))
        payload["createdAt"] = SERVER_TIMESTAMP
        msg_ref.set(payload, merge=True)
        print(f"💾 Saved message {message_id} to thread {thread_id}")
    except Exception as e:
        print(f"❌ Failed to save message {message_id}: {e}")

def index_message_id(user_id: str, message_id: str, thread_id: str):
    """Index message ID for O(1) lookup."""
    try:
        encoded_id = b64url_id(message_id)
        index_ref = _fs.collection("users").document(user_id).collection("msgIndex").document(encoded_id)
        index_ref.set({"threadId": thread_id}, merge=True)
        print(f"🔍 Indexed message ID: {message_id[:50]}... -> {thread_id}")
    except Exception as e:
        print(f"❌ Failed to index message {message_id}: {e}")

def lookup_thread_by_message_id(user_id: str, message_id: str) -> Optional[str]:
    """Look up thread ID by message ID."""
    try:
        encoded_id = b64url_id(message_id)
        doc = _fs.collection("users").document(user_id).collection("msgIndex").document(encoded_id).get()
        if doc.exists:
            return doc.to_dict().get("threadId")
        return None
    except Exception as e:
        print(f"❌ Failed to lookup message {message_id}: {e}")
        return None

def index_conversation_id(user_id: str, conversation_id: str, thread_id: str):
    """Index conversation ID for fallback lookup."""
    if not conversation_id:
        return
    try:
        conv_ref = _fs.collection("users").document(user_id).collection("convIndex").document(conversation_id)
        conv_ref.set({"threadId": thread_id}, merge=True)
        print(f"🔍 Indexed conversation ID: {conversation_id} -> {thread_id}")
    except Exception as e:
        print(f"❌ Failed to index conversation {conversation_id}: {e}")

def lookup_thread_by_conversation_id(user_id: str, conversation_id: str) -> Optional[str]:
    """Look up thread ID by conversation ID (fallback)."""
    if not conversation_id:
        return None
    try:
        doc = _fs.collection("users").document(user_id).collection("convIndex").document(conversation_id).get()
        if doc.exists:
            return doc.to_dict().get("threadId")
        return None
    except Exception as e:
        print(f"❌ Failed to lookup conversation {conversation_id}: {e}")
        return None

def exponential_backoff_request(func, max_retries: int = 3):
    """Execute request with exponential backoff on rate limits."""
    for attempt in range(max_retries):
        try:
            response = func()
            if response.status_code == 429:  # Rate limited
                retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                print(f"⏳ Rate limited, retrying after {retry_after}s")
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response
        except requests.exceptions.HTTPError as e:
            if e.response.status_code >= 500 and attempt < max_retries - 1:
                sleep_time = 2 ** attempt
                print(f"⏳ Server error, retrying after {sleep_time}s")
                time.sleep(sleep_time)
                continue
            raise
        except Exception as e:
            if attempt < max_retries - 1:
                sleep_time = 2 ** attempt
                print(f"⏳ Request failed, retrying after {sleep_time}s")
                time.sleep(sleep_time)
                continue
            raise
    raise Exception(f"Request failed after {max_retries} attempts")

# ─── Send and Index Email ──────────────────────────────

def send_and_index_email(user_id: str, headers: Dict[str, str], script: str, recipients: List[str], client_id_or_none: Optional[str] = None):
    """Send email and immediately index it in Firestore for reply tracking."""
    if not recipients:
        return {"sent": [], "errors": {"_all": "No recipients"}}

    content_type, content = _body_kind(script)
    results = {"sent": [], "errors": {}}
    base = "https://graph.microsoft.com/v1.0"

    for addr in recipients:
        msg = {
            "subject": "Client Outreach", 
            "body": {"contentType": content_type, "content": content},
            "toRecipients": [{"emailAddress": {"address": addr}}],
        }
        if client_id_or_none:
            msg["internetMessageHeaders"] = [{"name": "x-client-id", "value": client_id_or_none}]

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
            save_thread_root(user_id, root_id, thread_meta)
            
            # Message record
            message_record = {
                "direction": "outbound",
                "subject": subject,
                "from": "me",  # Graph doesn't return our own address easily
                "to": [addr],
                "sentDateTime": datetime.utcnow().isoformat() + "Z",
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

# ─── Scan Inbox and Match Replies ──────────────────────

def scan_inbox_against_index(user_id: str, headers: Dict[str, str], only_unread: bool = True, top: int = 50):
    """Scan inbox for replies and match against our Firestore index."""
    base = "https://graph.microsoft.com/v1.0"
    
    # Build filter
    filters = []
    if only_unread:
        filters.append("isRead eq false")
    
    filter_str = " and ".join(filters) if filters else ""
    
    params = {
        "$top": str(top),
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,toRecipients,receivedDateTime,sentDateTime,conversationId,internetMessageId,internetMessageHeaders,bodyPreview"
    }
    if filter_str:
        params["$filter"] = filter_str

    try:
        # Get inbox messages
        response = exponential_backoff_request(
            lambda: requests.get(f"{base}/me/mailFolders/Inbox/messages", headers=headers, params=params, timeout=30)
        )
        messages = response.json().get("value", [])
        
        print(f"📥 Found {len(messages)} inbox messages to process")
        
        for msg in messages:
            try:
                process_inbox_message(user_id, headers, msg)
            except Exception as e:
                print(f"❌ Failed to process message {msg.get('id', 'unknown')}: {e}")
                
    except Exception as e:
        print(f"❌ Failed to scan inbox: {e}")

def process_inbox_message(user_id: str, headers: Dict[str, str], msg: Dict[str, Any]):
    """Process a single inbox message for reply matching."""
    msg_id = msg.get("id")
    subject = msg.get("subject", "")
    from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "")
    internet_message_id = msg.get("internetMessageId")
    conversation_id = msg.get("conversationId")
    received_dt = msg.get("receivedDateTime")
    sent_dt = msg.get("sentDateTime")
    body_preview = msg.get("bodyPreview", "")
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
            "contentType": "Text",  # bodyPreview is always text
            "content": body_preview,
            "preview": safe_preview(body_preview)
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
    fetch_and_log_sheet_for_thread(user_id, thread_id, counterparty_email=from_addr)


def dump_thread_from_firestore(user_id: str, thread_id: str):
    """Console dump of thread conversation in chronological order."""
    try:
        print(f"\n📜 CONVERSATION THREAD: {thread_id}")
        print("=" * 80)
        
        # Get all messages in thread
        messages_ref = (_fs.collection("users").document(user_id)
                        .collection("threads").document(thread_id)
                        .collection("messages"))
        messages = list(messages_ref.stream())
        
        if not messages:
            print("(No messages found)")
            return
        
        # Sort by timestamp
        message_data = []
        for msg in messages:
            data = msg.to_dict()
            # Use sentDateTime for outbound, receivedDateTime for inbound
            timestamp = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(timestamp, 'timestamp'):
                timestamp = timestamp.timestamp()
            message_data.append((timestamp, data))
        
        message_data.sort(key=lambda x: x[0] if x[0] else 0)
        
        for timestamp, data in message_data:
            direction = data.get("direction", "unknown")
            subject = data.get("subject", "")
            from_addr = data.get("from", "")
            to_addrs = data.get("to", [])
            preview = data.get("body", {}).get("preview", "")
            
            if direction == "outbound":
                arrow = "ME → " + ", ".join(to_addrs)
            else:
                arrow = f"{from_addr} → ME"
            
            print(f"{arrow}")
            print(f"   Subject: {subject}")
            print(f"   Preview: {preview}")
            print()
        
        print("=" * 80)
        
    except Exception as e:
        print(f"❌ Failed to dump thread {thread_id}: {e}")

# ─── Modified Outbox Processing ────────────────────────

def send_outboxes(user_id: str, headers):
    """
    Modified to use send_and_index_email instead of send_email.
    """
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
                print(f"🗑️  Deleted outbox item {d.id}")
            else:
                attempts = int(data.get("attempts") or 0) + 1
                d.reference.set(
                    {"attempts": attempts, "lastError": json.dumps(res["errors"])[:1500]},
                    merge=True,
                )
                print(f"⚠️  Kept item {d.id} with error; attempts={attempts}")

        except Exception as e:
            attempts = int(data.get("attempts") or 0) + 1
            d.reference.set(
                {"attempts": attempts, "lastError": str(e)[:1500]},
                merge=True,
            )
            print(f"💥 Error sending item {d.id}: {e}; attempts={attempts}")

# ─── Legacy Functions (kept for compatibility) ──────────

def send_email(headers, script: str, emails: list[str], client_id: str | None = None):
    """Legacy function - redirects to send_and_index_email"""
    # Note: This legacy function doesn't have user_id, so it can't use the new pipeline
    # Users should migrate to send_and_index_email directly
    raise NotImplementedError("send_email is deprecated. Use send_and_index_email with user_id parameter.")

# ─── Utility: List user IDs from Firebase ──────────────
def list_user_ids():
    url = f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o?prefix=msal_caches%2F&key={FIREBASE_API_KEY}"
    r = requests.get(url)
    data = r.json()
    user_ids = set()
    for item in data.get("items", []):
        parts = item["name"].split("/")
        if len(parts) == 3 and parts[0] == "msal_caches" and parts[2] == "msal_token_cache.bin":
            user_ids.add(parts[1])
    return list(user_ids)

def decode_token_payload(token):
    payload = token.split(".")[1]
    padded = payload + '=' * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))

# ─── Email Functions ───────────────────────────────────
def send_weekly_email(headers, to_addresses):
    for addr in to_addresses:
        payload = {
            "message": {
                "subject": SUBJECT,
                "body": {"contentType": "Text", "content": BODY},
                "toRecipients": [{"emailAddress": {"address": addr}}]
            },
            "saveToSentItems": True
        }
        resp = requests.post("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=payload)
        resp.raise_for_status()
        print(f"✅ Sent '{SUBJECT}' to {addr}")

def process_replies(headers, user_id):
    url = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
    params = {
        '$filter': f"isRead eq false and startswith(subject,'Re: {SUBJECT}')",
        '$top': '10',
        '$orderby': 'receivedDateTime desc'
    }

    resp = requests.get(url, headers=headers, params=params)
    messages = resp.json().get("value", [])

    if not messages:
        print("ℹ️  No new replies.")
        return

    wb = Workbook()
    ws = wb.active
    ws.append(["Sender", "Response", "ReceivedDateTime"])

    for msg in messages:
        sender = msg["from"]["emailAddress"]["address"]
        body   = msg["body"]["content"].strip()
        dt     = msg["receivedDateTime"]

        reply_url = f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}/reply"
        reply_payload = {"message": {"body": {"contentType": "Text", "content": THANK_YOU_BODY}}}
        requests.post(reply_url, headers=headers, json=reply_payload)

        mark_read_url = f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}"
        requests.patch(mark_read_url, headers=headers, json={"isRead": True})

        ws.append([sender, body, dt])
        print(f"📥 Replied to and logged reply from {sender}")

    file = f"responses_{user_id}.xlsx"
    wb.save(file)
    upload_excel(FIREBASE_API_KEY, input_file=file)
    print(f"✅ Saved replies to {file}")

# ─── Main Loop ─────────────────────────────────────────
def refresh_and_process_user(user_id: str):
    print(f"\n🔄 Processing user: {user_id}")

    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)

    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as f:
        cache.deserialize(f.read())

    def _save_cache():
        if cache.has_state_changed:
            with open(TOKEN_CACHE, "w") as f:
                f.write(cache.serialize())
            upload_token(FIREBASE_API_KEY, input_file=TOKEN_CACHE, user_id=user_id)
            print(f"✅ Token cache uploaded for {user_id}")

    atexit.unregister(_save_cache)
    atexit.register(_save_cache)

    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if not accounts:
        print(f"⚠️ No account found for {user_id}")
        return

    # --- KEY CHANGE: do NOT force refresh; let MSAL use cached AT first ---
    before_state = cache.has_state_changed  # usually False right after deserialize
    result = app.acquire_token_silent(SCOPES, account=accounts[0])  # <-- no force_refresh
    after_state = cache.has_state_changed

    if not result or "access_token" not in result:
        print(f"❌ Silent auth failed for {user_id}")
        return

    access_token = result["access_token"]

    # Helpful logging: was it cached or refreshed?
    token_source = "refreshed_via_refresh_token" if (not before_state and after_state) else "cached_access_token"
    exp_secs = result.get("expires_in")
    print(f"🎯 Using {token_source}; expires_in≈{exp_secs}s — preview: {access_token[:40]}")

    # (Optional) sanity check on JWT-shaped token & appid
    if access_token.count(".") == 2:
        decoded = decode_token_payload(access_token)
        appid = decoded.get("appid", "unknown")
        if not appid.startswith("54cec"):
            print(f"⚠️ Unexpected appid: {appid}")
        else:
            print("✅ Token appid matches expected prefix")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Process outbound emails (now with indexing)
    send_outboxes(user_id, headers)
    
    # Scan for reply matches
    print(f"\n🔍 Scanning inbox for replies...")
    scan_inbox_against_index(user_id, headers, only_unread=True, top=50)

# ─── Entry ─────────────────────────────────────────────
if __name__ == "__main__":
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    for uid in all_users:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))