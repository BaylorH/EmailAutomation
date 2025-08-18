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
import hashlib
from typing import Optional, List, Dict, Any

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from datetime import datetime, timezone

import openai

# ─── Config ─────────────────────────────────────────────
CLIENT_ID         = os.getenv("AZURE_API_APP_ID")
CLIENT_SECRET     = os.getenv("AZURE_API_CLIENT_SECRET")
FIREBASE_API_KEY  = os.getenv("FIREBASE_API_KEY")
FIREBASE_BUCKET   = "email-automation-cache.firebasestorage.app"
AUTHORITY         = "https://login.microsoftonline.com/common"
SCOPES            = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE       = "msal_token_cache.bin"

# OpenAI config
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ASSISTANT_MODEL = os.getenv("OPENAI_ASSISTANT_MODEL", "gpt-4o")

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

if not OPENAI_API_KEY:
    raise RuntimeError("❌ Missing OPENAI_API_KEY env var")

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

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

def _col_letter(n: int) -> str:
    """1-indexed column number -> A1 letter (1->A)."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s

def _header_index_map(header: list[str]) -> dict:
    """Normalize headers for exact match regardless of spacing/case."""
    return { (h or "").strip().lower(): i for i, h in enumerate(header, start=1) }  # 1-based

def apply_proposal_to_sheet(
    uid: str,
    client_id: str,
    sheet_id: str,
    header: list[str],
    rownum: int,
    current_rowvals: list[str],
    proposal: dict,
) -> dict:
    """
    Applies proposal['updates'] to the sheet row.
    Returns {"applied":[...], "skipped":[...]} items with old/new values.
    """
    try:
        sheets = _sheets_client()
        tab_title = _get_first_tab_title(sheets, sheet_id)

        if not proposal or not isinstance(proposal.get("updates"), list):
            return {"applied": [], "skipped": [{"reason":"no-updates"}]}

        idx_map = _header_index_map(header)

        data_payload = []
        applied, skipped = [], []

        for upd in proposal["updates"]:
            col_name = (upd.get("column") or "").strip()
            new_val  = "" if upd.get("value") is None else str(upd.get("value"))
            conf     = upd.get("confidence")
            reason   = upd.get("reason")

            key = col_name.strip().lower()
            if key not in idx_map:
                skipped.append({"column": col_name, "reason": "unknown header"})
                continue

            col_idx = idx_map[key]                     # 1-based
            col_letter = _col_letter(col_idx)          # A1
            rng = f"{tab_title}!{col_letter}{rownum}"

            old_val = current_rowvals[col_idx-1] if (col_idx-1) < len(current_rowvals) else ""

            data_payload.append({"range": rng, "values": [[new_val]]})
            applied.append({
                "column": col_name,
                "range": rng,
                "oldValue": old_val,
                "newValue": new_val,
                "confidence": conf,
                "reason": reason,
            })

        if not data_payload:
            return {"applied": [], "skipped": skipped}

        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": "RAW",
                "data": data_payload
            }
        ).execute()

        # mark success
        return {"applied": applied, "skipped": skipped}

    except Exception as e:
        print(f"❌ Failed to apply proposal to sheet: {e}")
        return {"applied": [], "skipped": [{"reason": f"exception: {e}"}]}


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


# ─── NEW: Task A - Assistant Management ─────────────────────────

def get_or_create_assistant(uid: str, client_id: str, email: str, sheet_id: str) -> dict:
    """
    Returns {"assistantId": str, "docRef": <Firestore ref>, "doc": dict}.
    1) Compute key = f"{client_id}__{email.lower()}".
    2) If doc exists, return it and update lastUsedAt.
    3) Else create an OpenAI Assistant (model = env OPENAI_ASSISTANT_MODEL or "gpt-4o"),
       with tools=[{"type":"code_interpreter"}], no files for now.
    4) Persist doc with assistantId, clientId, email, sheetId, fileIds=[].
    """
    doc_id = f"{client_id}__{email.lower()}"
    doc_ref = _fs.collection("users").document(uid).collection("assistantIndex").document(doc_id)
    
    # Check if doc exists
    doc_snapshot = doc_ref.get()
    
    if doc_snapshot.exists:
        # Update lastUsedAt and return existing
        doc_ref.update({"lastUsedAt": SERVER_TIMESTAMP})
        doc_data = doc_snapshot.to_dict()
        assistant_id = doc_data.get("assistantId")
        print(f"🔄 Reusing assistant {assistant_id} for {client_id}__{email.lower()}")
        return {
            "assistantId": assistant_id,
            "docRef": doc_ref,
            "doc": doc_data
        }
    else:
        # Create new OpenAI Assistant
        try:
            assistant = openai_client.beta.assistants.create(
                name=f"Sheet Assistant for {client_id}",
                model=OPENAI_ASSISTANT_MODEL,
                tools=[{"type": "code_interpreter"}],
                tool_resources={"code_interpreter": {"file_ids": []}}
            )
            assistant_id = assistant.id
            
            # Create Firestore doc
            doc_data = {
                "assistantId": assistant_id,
                "clientId": client_id,
                "email": email.lower(),
                "sheetId": sheet_id,
                "fileIds": [],
                "model": OPENAI_ASSISTANT_MODEL,
                "createdAt": SERVER_TIMESTAMP,
                "lastUsedAt": SERVER_TIMESTAMP
            }
            
            doc_ref.set(doc_data)
            print(f"🆕 Created assistant {assistant_id} for {client_id}__{email.lower()}")
            
            return {
                "assistantId": assistant_id,
                "docRef": doc_ref,
                "doc": doc_data
            }
            
        except Exception as e:
            print(f"❌ Failed to create OpenAI assistant: {e}")
            raise


# ─── NEW: Task B - Test Sheet Write ─────────────────────────────

def _ensure_log_tab_exists(sheets, spreadsheet_id: str) -> str:
    """Ensure 'Log' tab exists and return its title."""
    try:
        meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_names = [sheet["properties"]["title"] for sheet in meta["sheets"]]
        
        if "Log" in sheet_names:
            return "Log"
        
        # Create Log tab
        request = {
            "requests": [{
                "addSheet": {
                    "properties": {
                        "title": "Log"
                    }
                }
            }]
        }
        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request).execute()
        print("📋 Created 'Log' tab")
        return "Log"
        
    except Exception as e:
        print(f"⚠️ Could not create Log tab: {e}")
        # Fallback to first tab
        meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        return meta["sheets"][0]["properties"]["title"]


def _get_thread_messages_chronological(uid: str, thread_id: str) -> list[dict]:
    """Get all messages in thread in chronological order."""
    try:
        messages_ref = (_fs.collection("users").document(uid)
                        .collection("threads").document(thread_id)
                        .collection("messages"))
        messages = list(messages_ref.stream())
        
        if not messages:
            return []
        
        # Sort by timestamp
        message_data = []
        for msg in messages:
            data = msg.to_dict()
            # Use sentDateTime for outbound, receivedDateTime for inbound
            timestamp = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(timestamp, 'timestamp'):
                timestamp = timestamp.timestamp()
            elif isinstance(timestamp, str):
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    timestamp = dt.timestamp()
                except:
                    timestamp = 0
            else:
                timestamp = 0
                
            message_data.append((timestamp, data, msg.id))
        
        message_data.sort(key=lambda x: x[0])
        return [{"data": data, "id": msg_id} for _, data, msg_id in message_data]
        
    except Exception as e:
        print(f"❌ Failed to get thread messages: {e}")
        return []


def _get_last_logged_message_id(sheets, spreadsheet_id: str, tab_title: str, thread_id: str) -> str | None:
    """Get the last message ID that was logged for this thread."""
    try:
        # Read all values from Log tab
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A:H"
        ).execute()
        
        rows = resp.get("values", [])
        if not rows:
            return None
        
        # Look for the most recent block for this thread_id
        last_message_id = None
        i = len(rows) - 1
        
        while i >= 0:
            row = rows[i]
            if len(row) >= 3 and row[1] == thread_id:
                # This is a message row for our thread
                # The last column should contain message ID or be empty for summary row
                if len(row) >= 8 and row[7]:  # Message ID in column H
                    last_message_id = row[7]
                    break
            i -= 1
            
        return last_message_id
        
    except Exception as e:
        print(f"⚠️ Could not check last logged message: {e}")
        return None


def write_message_order_test(uid: str, thread_id: str, sheet_id: str):
    """
    1) Read the first tab title.
    2) Create (if missing) a tab named 'Log' (case-sensitive).
    3) Build a chronological list of this thread's messages from Firestore
       (direction, from/to, subject, preview, received/sent time).
    4) Append a single new row to 'Log':
         [ iso_now, thread_id, "message_count=<N>", "emails=<counterpartyEmail>", "clientId=<clientId>" ]
       Then append N additional rows (one per message) like:
         [ "", "", "<idx>", "<direction>", "<from>", "<joined_to>", "<subject>", "<preview>" ]
    Idempotency: if the immediately previous appended block has the same last message id,
                 skip appending and log 'already logged'.
    """
    try:
        sheets = _sheets_client()
        
        # Get thread info
        thread_doc = _fs.collection("users").document(uid).collection("threads").document(thread_id).get()
        if not thread_doc.exists:
            print(f"⚠️ Thread {thread_id} not found for logging")
            return
            
        thread_data = thread_doc.to_dict() or {}
        client_id = thread_data.get("clientId", "unknown")
        emails = thread_data.get("email", [])
        counterparty_email = emails[0] if emails else "unknown"
        
        # Ensure Log tab exists
        log_tab = _ensure_log_tab_exists(sheets, sheet_id)
        
        # Get chronological messages
        messages = _get_thread_messages_chronological(uid, thread_id)
        if not messages:
            print(f"ℹ️ No messages found for thread {thread_id}")
            return
            
        # Check idempotency - compare with last logged message ID
        last_logged_id = _get_last_logged_message_id(sheets, sheet_id, log_tab, thread_id)
        current_last_id = messages[-1]["id"] if messages else None
        
        if last_logged_id == current_last_id:
            print(f"✅ Already logged; same last message id: {last_logged_id}")
            return
        
        # Prepare rows to append
        # now_iso = datetime.utcnow().isoformat() + "Z"
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()  # ends with +00:00

        message_count = len(messages)
        
        # Summary row
        rows_to_append = [[
            now_iso,
            thread_id,
            f"message_count={message_count}",
            f"emails={counterparty_email}",
            f"clientId={client_id}",
            "",  # Empty columns
            "",
            ""
        ]]
        
        # Message rows
        for idx, msg_info in enumerate(messages, 1):
            data = msg_info["data"]
            msg_id = msg_info["id"]
            
            direction = data.get("direction", "unknown")
            from_addr = data.get("from", "")
            to_addrs = data.get("to", [])
            joined_to = ", ".join(to_addrs) if to_addrs else ""
            subject = data.get("subject", "")
            preview = data.get("body", {}).get("preview", "")[:100]  # Limit preview length
            
            rows_to_append.append([
                "",  # Empty timestamp for message rows
                "",  # Empty thread_id for message rows  
                str(idx),
                direction,
                from_addr,
                joined_to,
                subject,
                msg_id  # Store message ID for idempotency checking
            ])
        
        # Append to sheet
        request_body = {
            "values": rows_to_append
        }
        
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{log_tab}!A:H",
            valueInputOption="RAW",
            body=request_body
        ).execute()
        
        print(f"📝 Logged {message_count} messages to '{log_tab}' tab for thread {thread_id}")
        
    except Exception as e:
        print(f"❌ Failed to write message order test: {e}")


# ─── NEW: Task C - GPT Proposal Scaffolding ─────────────────────

def build_conversation_payload(uid: str, thread_id: str, limit: int = 10) -> list[dict]:
    """
    Return last N messages in chronological order, each item:
    { "direction": "inbound"|"outbound", "from": str, "to": [str],
      "subject": str, "timestamp": iso, "preview": str }
    """
    try:
        messages = _get_thread_messages_chronological(uid, thread_id)
        
        # Take last N messages
        recent_messages = messages[-limit:] if len(messages) > limit else messages
        
        payload = []
        for msg_info in recent_messages:
            data = msg_info["data"]
            
            # Get timestamp (prefer sent/received, fallback to created)
            timestamp = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(timestamp, 'isoformat'):
                timestamp = timestamp.isoformat()
            elif not isinstance(timestamp, str):
                timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


            payload.append({
                "direction": data.get("direction", "unknown"),
                "from": data.get("from", ""),
                "to": data.get("to", []),
                "subject": data.get("subject", ""),
                "timestamp": timestamp,
                "preview": data.get("body", {}).get("preview", "")[:200]  # Limit for token budget
            })
        
        return payload
        
    except Exception as e:
        print(f"❌ Failed to build conversation payload: {e}")
        return []


def propose_sheet_updates(uid: str, client_id: str, email: str, sheet_id: str, header: list[str],
                          rownum: int, rowvals: list[str], thread_id: str) -> dict | None:
    """
    - Uses get_or_create_assistant(...) to get assistantId (store/refresh lastUsedAt).
    - Build a single 'user' message for OpenAI that contains:
        * Header (row 2)
        * Current values for the matched row
        * Conversation payload from build_conversation_payload(...)
        * Strict instruction to output JSON with shape:
          {
            "updates":[{"column": "<header name>", "value": "<string>", "confidence": 0..1, "reason": "<why>"}],
            "notes": "<optional>"
          }
      and *only* that JSON as output.
    - Parse JSON safely; on failure, log and return None.
    - Log the proposal (pretty-printed) to console and also store a record in:
      users/{uid}/sheetChangeLog/{threadId}__{iso}
        - clientId, email, sheetId, rowNumber, proposalJson, proposalHash, status="proposed"
    - Do NOT write to the sheet yet.
    """
    try:
        # Get or create assistant
        assistant_info = get_or_create_assistant(uid, client_id, email, sheet_id)
        assistant_id = assistant_info["assistantId"]
        
        # Build conversation payload
        conversation = build_conversation_payload(uid, thread_id, limit=10)
        
        # Build prompt for OpenAI
        prompt = f"""
You are analyzing a conversation thread to suggest updates to a Google Sheet row.

SHEET HEADER (row 2):
{json.dumps(header)}

CURRENT ROW VALUES (row {rownum}):
{json.dumps(rowvals)}

CONVERSATION HISTORY:
{json.dumps(conversation, indent=2)}

Based on this conversation, suggest updates to the sheet row. Consider:
- New information revealed in the conversation
- Status changes or updates mentioned
- Contact information updates
- Progress or milestone updates

OUTPUT ONLY valid JSON in this exact format:
{{
  "updates": [
    {{
      "column": "<exact header name>",
      "value": "<new value as string>",
      "confidence": 0.85,
      "reason": "<brief explanation why this update is suggested>"
    }}
  ],
  "notes": "<optional general notes about the conversation>"
}}

Be conservative with updates. Only suggest changes where you have good confidence based on explicit information in the conversation.
"""

        # Call OpenAI (using simple completion, not assistants API for this)
        response = openai_client.chat.completions.create(
            model=OPENAI_ASSISTANT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1000
        )
        
        raw_response = response.choices[0].message.content.strip()
        
        # Parse JSON safely
        try:
            # Handle potential code fences
            if raw_response.startswith("```"):
                lines = raw_response.split("\n")
                json_lines = []
                in_json = False
                for line in lines:
                    if line.strip().startswith("```"):
                        in_json = not in_json
                        continue
                    if in_json:
                        json_lines.append(line)
                raw_response = "\n".join(json_lines)
            
            proposal = json.loads(raw_response)
            
        except json.JSONDecodeError as e:
            print(f"❌ Failed to parse GPT JSON response: {e}")
            print(f"Raw response: {raw_response}")
            return None
        
        # Validate JSON structure
        if not isinstance(proposal, dict) or "updates" not in proposal:
            print(f"❌ Invalid proposal structure: {proposal}")
            return None
        
        # Log the proposal
        print(f"\n🤖 GPT Proposal for {client_id}__{email}:")
        print(json.dumps(proposal, indent=2))
        
        # Store in sheetChangeLog
        # now_iso = datetime.utcnow().isoformat().replace(":", "-").replace(".", "-") + "Z"
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()  # ends with +00:00

        # log_doc_id = f"{thread_id}__{now_iso}"

        log_doc_id = f"{thread_id}__{now_utc.isoformat().replace(':','-').replace('.','-').replace('+00:00','Z')}"

        
        proposal_hash = hashlib.sha256(
            json.dumps(proposal, sort_keys=True).encode('utf-8')
        ).hexdigest()[:16]
        
        change_log_data = {
            "clientId": client_id,
            "email": email,
            "sheetId": sheet_id,
            "rowNumber": rownum,
            "proposalJson": proposal,
            "proposalHash": proposal_hash,
            "status": "proposed",
            "threadId": thread_id,
            "assistantId": assistant_id,
            "createdAt": SERVER_TIMESTAMP
        }
        
        _fs.collection("users").document(uid).collection("sheetChangeLog").document(log_doc_id).set(change_log_data)
        print(f"💾 Stored proposal in sheetChangeLog/{log_doc_id}")
        
        return proposal
        
    except Exception as e:
        print(f"❌ Failed to propose sheet updates: {e}")
        return None


# ─── EXISTING FUNCTIONS (updated to integrate new features) ─────────

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

    print(f"📄 Sheet fetched: title='{tab_title}', sheetId={sheet_id}")
    print(f"   Header (row 2): {header}")
    print(f"   Counterparty email (row match): {counterparty_email or 'unknown'}")

    # Find the row matching the counterparty email and print it
    rownum, rowvals = _find_row_by_email(sheets, sheet_id, tab_title, header, counterparty_email or "")
    if rownum is not None:
        print(f"📌 Matched row {rownum}: {rowvals}")
        return client_id, sheet_id, header, rownum, rowvals
    else:
        # Be loud – row must exist for our workflow
        print(f"❌ No sheet row found with email = {counterparty_email}")
        return client_id, sheet_id, header, None, None


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

# ─── Scan Inbox and Match Replies ──────────────────────

# Add these new helper functions after the existing Firestore helpers

def add_client_notifications(
    uid: str,
    client_id: str,
    email: str,
    thread_id: str,
    applied_updates: list[dict],
    notes: str | None = None,
):
    """
    Writes one notification doc per run under:
      users/{uid}/clients/{client_id}/notifications/{autoId}

    Also updates a small summary on the client doc for quick dashboards.
    """
    try:
        base_ref = _fs.collection("users").document(uid)
        client_ref = base_ref.collection("clients").document(client_id)
        notif_ref = client_ref.collection("notifications").document()

        summary_items = [f"{u['column']}='{u['newValue']}'" for u in applied_updates]
        summary = f"Updated {', '.join(summary_items)} for {email}" if summary_items else "No updates applied"

        payload = {
            "type": "sheet_update",
            "email": (email or "").lower(),
            "threadId": thread_id,
            "applied": applied_updates,   # [{column, oldValue, newValue, confidence, reason, range}]
            "notes": notes or "",
            "createdAt": SERVER_TIMESTAMP,
        }
        notif_ref.set(payload)

        # light summary on the client document (cheap to read in UI)
        client_ref.set({
            "lastNotificationSummary": summary,
            "lastNotificationAt": SERVER_TIMESTAMP,
        }, merge=True)

        print(f"🔔 Notification stored for client {client_id}: {summary}")

    except Exception as e:
        print(f"❌ Failed to write client notification: {e}")


def _processed_ref(user_id: str, key: str):
    """Get reference to processed message document."""
    encoded_key = b64url_id(key)
    return _fs.collection("users").document(user_id).collection("processedMessages").document(encoded_key)

def has_processed(user_id: str, key: str) -> bool:
    """Check if a message has already been processed."""
    try:
        doc = _processed_ref(user_id, key).get()
        return doc.exists
    except Exception as e:
        print(f"❌ Failed to check processed status for {key}: {e}")
        return False

def mark_processed(user_id: str, key: str):
    """Mark a message as processed."""
    try:
        _processed_ref(user_id, key).set({
            "processedAt": SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"❌ Failed to mark message as processed {key}: {e}")

def _sync_ref(user_id: str):
    """Get reference to sync document."""
    return _fs.collection("users").document(user_id).collection("sync").document("inbox")

def get_last_scan_iso(user_id: str) -> str | None:
    """Get the last scan timestamp."""
    try:
        doc = _sync_ref(user_id).get()
        if doc.exists:
            return doc.to_dict().get("lastScanISO")
        return None
    except Exception as e:
        print(f"❌ Failed to get last scan ISO: {e}")
        return None

def set_last_scan_iso(user_id: str, iso_str: str):
    """Set the last scan timestamp."""
    try:
        _sync_ref(user_id).set({
            "lastScanISO": iso_str,
            "updatedAt": SERVER_TIMESTAMP
        }, merge=True)
    except Exception as e:
        print(f"❌ Failed to set last scan ISO: {e}")

# Replace the existing scan_inbox_against_index function with this:

def scan_inbox_against_index(user_id: str, headers: Dict[str, str], only_unread: bool = True, top: int = 50):
    """Idempotent scan of inbox for replies with early exit on processed messages."""
    base = "https://graph.microsoft.com/v1.0"
    
    # Calculate 5-hour cutoff
    from datetime import datetime, timedelta
    # now_utc = datetime.utcnow()
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
    client_id, sheet_id, header, rownum, rowvals = fetch_and_log_sheet_for_thread(user_id, thread_id, counterparty_email=from_addr)
    
    # Only proceed if we successfully matched a sheet row
    if sheet_id and rownum is not None:
        from_addr_lower = (from_addr or "").lower()
        
        # Step 2: test write
        write_message_order_test(user_id, thread_id, sheet_id)
        
        # Step 3: get GPT proposal (no writes yet)
        proposal = propose_sheet_updates(user_id, client_id, from_addr_lower, sheet_id, header, rownum, rowvals, thread_id)
        if proposal and proposal.get("updates"):
            apply_result = apply_proposal_to_sheet(
                user_id, client_id, sheet_id, header, rownum, rowvals, proposal
            )

            # optional: store an "applied" record in sheetChangeLog too
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
                    "assistantId": get_or_create_assistant(user_id, client_id, from_addr_lower, sheet_id)["assistantId"],
                    "proposalHash": applied_hash,
                })
            except Exception as e:
                print(f"⚠️ Failed to store applied record: {e}")

            # write client notification
            add_client_notifications(
                user_id, client_id, from_addr_lower, thread_id,
                applied_updates=apply_result.get("applied", []),
                notes=proposal.get("notes")
            )
        else:
            print("ℹ️ No proposal or no updates; nothing to apply.")



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