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
from bs4 import BeautifulSoup
from google.cloud.firestore_v1 import FieldFilter

# Config
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

# Required fields for closing conversations
REQUIRED_FIELDS_FOR_CLOSE = [
    "Total SF","Rent/SF /Yr","Ops Ex /SF","Gross Rent",
    "Drive Ins","Docks","Ceiling Ht","Power"
]

if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
    raise RuntimeError("Missing required env vars")

if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY env var")

# Initialize OpenAI client
openai.api_key = OPENAI_API_KEY
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Firestore Admin client (uses GOOGLE_APPLICATION_CREDENTIALS)
_fs = firestore.Client()

# Helper: detect HTML vs text
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
        raise RuntimeError("Missing GOOGLE_OAUTH_CLIENT_ID/SECRET/REFRESH_TOKEN")

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
    doc_ref = _fs.collection("users").document(uid).collection("clients").document(client_id)
    doc_snapshot = doc_ref.get()
    if doc_snapshot.exists:
        doc_data = doc_snapshot.to_dict() or {}
        sid = doc_data.get("sheetId")
        if sid:
            return sid

    # Try archived clients (emails might keep flowing after archive)
    archived_doc_ref = _fs.collection("users").document(uid).collection("archivedClients").document(client_id)
    archived_doc_snapshot = archived_doc_ref.get()
    if archived_doc_snapshot.exists:
        archived_doc_data = archived_doc_snapshot.to_dict() or {}
        sid = archived_doc_data.get("sheetId")
        if sid:
            return sid

    # Required by design → fail loudly
    raise RuntimeError(f"sheetId not found for uid={uid} clientId={client_id}. This field is required.")


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

def _first_sheet_props(sheets, spreadsheet_id):
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    p = meta["sheets"][0]["properties"]
    return p["sheetId"], p["title"]

def _approx_header_px(text: str) -> int:
    # rough width estimate for default Sheets font (keeps headers visible)
    if not text:
        return 80
    px = int(len(text) * 7 + 24)  # chars * avg px + padding
    return max(100, min(px, 1000))

def _ensure_divider_conditional_formatting(sheets, spreadsheet_id: str):
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheet = meta["sheets"][0]
    sheet_id = sheet["properties"]["sheetId"]

    # Remove any existing identical rules (optional tidy, safe to skip)
    # You can fetch existing CF rules via spreadsheets().get and clean if you want.

    # Apply: from row 3 down, across all columns we care about.
    add_rule = {
        "requests": [{
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 2,  # row 3 (0-based)
                        "startColumnIndex": 0,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$A3="NON-VIABLE"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.8, "green": 0.0, "blue": 0.0},
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                            }
                        }
                    }
                },
                "index": 0
            }
        }]
    }
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body=add_rule
    ).execute()


def format_sheet_columns_autosize_with_exceptions(spreadsheet_id: str, header: list[str]) -> None:
    """
    Auto-size all columns to the longest visible value + padding, with exceptions:
      - 'Listing Brokers Comments ' and 'Jill and Clients Comments' -> WRAP and be reasonably wide
      - 'Flyer / Link' and 'Floorplan' -> CLIP and keep width small (ignore huge URLs)
    Header row (row 2) is NOT wrapped.
    Column A additionally respects the width of cell A1 (client name).
    """
    # --- Tunables --------------------------------------------------------------
    CHAR_PX          = 8
    BASE_PADDING_PX  = 24
    EXTRA_FUDGE_PX   = 6

    MIN_WRAP_PX      = 280
    MAX_WRAP_PX      = 600

    LINK_MIN_PX      = 140
    LINK_CAP_PX      = 240
    LINK_HALF_FACTOR = 0.5

    MIN_ANY_PX       = 80
    MAX_ANY_PX       = 900
    # --------------------------------------------------------------------------

    def _norm(name: str) -> str:
        return (name or "").strip().lower()

    WRAP_KEYS = {
        "listing brokers comments",   # trailing space in sheet header is normalized out
        "jill and clients comments",
    }
    LINK_KEYS = {"flyer / link", "floorplan", "floor plan"}

    sheets = _sheets_client()
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    first_sheet = meta["sheets"][0]
    grid_id     = first_sheet["properties"]["sheetId"]
    tab_title   = first_sheet["properties"]["title"]

    # Get A1 (client name) so col A can respect it
    a1_val = ""
    try:
        a1_resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A1:A1"
        ).execute()
        a1_vals = a1_resp.get("values", [])
        if a1_vals and a1_vals[0]:
            a1_val = str(a1_vals[0][0]) or ""
    except Exception as _:
        a1_val = ""

    a1_px = len(a1_val) * CHAR_PX + BASE_PADDING_PX + EXTRA_FUDGE_PX if a1_val else 0

    # Read header row (2) + data (rows 3+)
    values_resp = sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab_title}!A2:ZZZ"
    ).execute()
    rows = values_resp.get("values", [])
    hdr  = rows[0] if rows else header
    data = rows[1:] if len(rows) > 1 else []

    num_cols = max(len(hdr), len(header))
    requests = []

    for c in range(num_cols):
        header_text = (hdr[c] if c < len(hdr) else (header[c] if c < len(header) else "")) or ""
        header_len  = len(header_text)
        header_px   = header_len * CHAR_PX + BASE_PADDING_PX + EXTRA_FUDGE_PX

        # Longest content in data rows
        max_len = header_len
        for r in data:
            if c < len(r) and r[c]:
                L = len(str(r[c]))
                if L > max_len:
                    max_len = L

        auto_px = max_len * CHAR_PX + BASE_PADDING_PX + EXTRA_FUDGE_PX
        col_key = _norm(header_text)

        # --- width/wrap policy by column type
        if col_key in LINK_KEYS:
            width_px = int(auto_px * LINK_HALF_FACTOR)
            width_px = max(width_px, max(header_px, LINK_MIN_PX))
            width_px = min(width_px, LINK_CAP_PX)
            wrap_mode = "CLIP"

        elif col_key in WRAP_KEYS:
            width_px = max(MIN_WRAP_PX, min(auto_px, MAX_WRAP_PX))
            wrap_mode = "WRAP"

        else:
            width_px = max(header_px, auto_px)
            wrap_mode = "OVERFLOW_CELL"

        # NEW: ensure column A is at least wide enough for A1 (client name)
        if c == 0:
            width_px = max(width_px, a1_px)

        # clamp final width
        width_px = max(MIN_ANY_PX, min(int(width_px), MAX_ANY_PX))

        # 1) set column width
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": grid_id, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                "properties": {"pixelSize": int(width_px)},
                "fields": "pixelSize"
            }
        })

        # 2) wrap strategy for DATA ONLY (row 3+); header row stays unwrapped
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": grid_id,
                    "startRowIndex": 2,
                    "startColumnIndex": c,
                    "endColumnIndex": c + 1
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": wrap_mode}},
                "fields": "userEnteredFormat.wrapStrategy"
            }
        })

    if requests:
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}
        ).execute()


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

# --- NEW: Notifications System ---

def write_notification(uid: str, client_id: str, *, kind: str, priority: str, email: str, 
                      thread_id: str, row_number: int = None, row_anchor: str = None, 
                      meta: dict = None, dedupe_key: str = None) -> str:
    """
    Write notification and bump counters atomically.
    Returns the notification document ID.
    """
    try:
        # Use dedupe_key as doc ID if provided
        if dedupe_key:
            doc_id = hashlib.sha1(dedupe_key.encode('utf-8')).hexdigest()
        else:
            doc_id = None  # Let Firestore auto-generate
        
        client_ref = _fs.collection("users").document(uid).collection("clients").document(client_id)
        # If doc_id is fixed (dedupe), we can safely create a stable ref now
        notif_ref = (client_ref.collection("notifications").document(doc_id)
                     if doc_id else client_ref.collection("notifications").document())

        notification_doc = {
            "kind": kind,
            "priority": priority,
            "email": email,
            "threadId": thread_id,
            "rowNumber": row_number,
            "rowAnchor": row_anchor,
            "createdAt": SERVER_TIMESTAMP,
            "meta": meta or {},
            "dedupeKey": dedupe_key
        }

        @firestore.transactional
        def update_with_counters(transaction):
            # READS FIRST
            client_snapshot = client_ref.get(transaction=transaction)

            # Dedupe check must also be a READ before any WRITE
            if dedupe_key:
                notif_snapshot = notif_ref.get(transaction=transaction)
                if notif_snapshot.exists:
                    print(f"📋 Skipped duplicate notification: {dedupe_key}")
                    return notif_ref.id  # No-op

            current_data = client_snapshot.to_dict() if client_snapshot.exists else {}
            unread_count = (current_data.get("notificationsUnread") or 0) + 1
            new_update_count = (current_data.get("newUpdateCount") or 0)
            notif_counts = dict(current_data.get("notifCounts") or {})

            if kind == "sheet_update":
                new_update_count += 1
            notif_counts[kind] = notif_counts.get(kind, 0) + 1

            # WRITES AFTER ALL READS
            transaction.set(notif_ref, notification_doc)
            transaction.set(
                client_ref,
                {
                    "notificationsUnread": unread_count,
                    "newUpdateCount": new_update_count,
                    "notifCounts": notif_counts
                },
                merge=True
            )
            return notif_ref.id

        transaction = _fs.transaction()
        created_id = update_with_counters(transaction)
        print(f"📋 Created {kind} notification for {client_id}: {created_id}")
        return created_id

    except Exception as e:
        print(f"❌ Failed to write notification: {e}")
        raise


# --- NEW: URL Exploration ---

def fetch_url_as_text(url: str) -> str | None:
    """
    Try to fetch URL content and extract visible text using BeautifulSoup.
    Returns None on any failure (fail-safe).
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        response.raise_for_status()
        
        # Parse with BeautifulSoup
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style"]):
            script.decompose()
        
        # Get text
        text = soup.get_text()
        
        # Clean up whitespace
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)
        
        # Limit size
        if len(text) > 5000:
            text = text[:5000] + "..."
        
        print(f"🌐 Fetched {len(text)} chars from {url}")
        return text
        
    except Exception as e:
        print(f"⚠️ Failed to fetch URL {url}: {e}")
        return None

# --- NEW: Non-viable divider and row operations ---

def _ensure_divider_conditional_formatting(sheets, spreadsheet_id: str) -> None:
    """
    Add a conditional formatting rule that paints ANY row red + bold white text
    when column A equals 'NON-VIABLE'. Idempotent enough for repeated calls.
    """
    # Figure out sheet + a reasonable column span
    meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    first = meta["sheets"][0]
    sheet_id = first["properties"]["sheetId"]
    tab_title = first["properties"]["title"]

    # Use header width to decide how many columns to cover (fallback to 26)
    try:
        header_resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!2:2"
        ).execute()
        header = header_resp.get("values", [[]])[0] if header_resp.get("values") else []
        num_cols = max(26, len(header) or 0)
    except Exception:
        num_cols = 26

    # Apply rule from row 3 downward (data rows), across detected columns
    add_rule = {
        "requests": [{
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 2,          # row 3 (0-based)
                        "startColumnIndex": 0,
                        "endColumnIndex": num_cols
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": '=$A3="NON-VIABLE"'}]
                        },
                        "format": {
                            "backgroundColor": {"red": 0.8, "green": 0.0, "blue": 0.0},
                            "textFormat": {
                                "bold": True,
                                "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}
                            }
                        }
                    }
                },
                "index": 0
            }
        }]
    }

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=add_rule
    ).execute()


def ensure_nonviable_divider(sheets, spreadsheet_id: str, tab_title: str) -> int:
    """
    Ensure a NON-VIABLE divider row exists. Returns the divider row number.
    Creates if missing by writing 'NON-VIABLE' in column A only and
    ensures conditional formatting is installed (no hard painting).
    """
    try:
        # Scan column A for existing divider
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A:A"
        ).execute()
        rows = resp.get("values", [])

        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).strip().upper() == "NON-VIABLE":
                # Make sure CF rule exists even if divider already present
                _ensure_divider_conditional_formatting(sheets, spreadsheet_id)
                print(f"📍 Found existing NON-VIABLE divider at row {i}")
                return i

        # Not found: create at the end by setting ONLY column A
        divider_row = (len(rows) + 1) if rows else 1
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A{divider_row}",
            valueInputOption="RAW",
            body={"values": [["NON-VIABLE"]]}
        ).execute()

        # Ensure the conditional formatting (styling follows the text)
        _ensure_divider_conditional_formatting(sheets, spreadsheet_id)

        print(f"🔴 Created NON-VIABLE divider at row {divider_row}")
        return divider_row

    except Exception as e:
        print(f"❌ Failed to ensure NON-VIABLE divider: {e}")
        raise


def move_row_below_divider(sheets, spreadsheet_id: str, tab_title: str, src_row: int, divider_row: int) -> int:
    """
    Move src_row to immediately below the divider *and* keep the divider as the boundary.
    Returns the new row number of the moved row (immediately below the divider after the operation).
    All row numbers are 1-based in the function signature.
    """
    try:
        sheet_id = _first_sheet_props(sheets, spreadsheet_id)[0]

        # 1) Insert one blank row immediately BELOW the divider (0-based index = divider_row)
        requests = [{
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": divider_row,      # 0-based; divider_row is 1-based → row below divider
                    "endIndex": divider_row + 1
                },
                "inheritFromBefore": False
            }
        }]

        # Count columns so we can copy across all used columns
        header = _read_header_row2(sheets, spreadsheet_id, tab_title)
        num_cols = max(1, len(header))

        # 2) COPY the source row to the new blank row just inserted (at divider_row+1 in 1-based terms)
        requests.append({
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": src_row - 1,
                    "endRowIndex": src_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": divider_row,   # the newly inserted blank row (0-based)
                    "endRowIndex": divider_row + 1,
                    "startColumnIndex": 0,
                    "endColumnIndex": num_cols
                },
                "pasteType": "PASTE_NORMAL"
            }
        })

        # 3) DELETE the original source row (above the divider), which lifts divider up by one
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": src_row - 1,
                    "endIndex": src_row
                }
            }
        })

        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()

        # After deletion of a row above the divider, the divider shifts up by 1.
        # The moved row sits immediately below the (new) divider.
        new_row = divider_row  # 1-based index of the moved row after the sequence
        print(f"📍 Moved row {src_row} below divider -> now at {new_row}")
        return new_row

    except Exception as e:
        print(f"❌ Failed to move row below divider: {e}")
        raise


def insert_property_row_above_divider(sheets, spreadsheet_id: str, tab_title: str, values_by_header: dict) -> int:
    """
    Insert a new property row one row above the divider (or at end if no divider).
    Returns the new row number.
    """
    try:
        header = _read_header_row2(sheets, spreadsheet_id, tab_title)
        
        # Find divider
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"{tab_title}!A:A"
        ).execute()
        rows = resp.get("values", [])
        
        divider_row = None
        for i, row in enumerate(rows, start=1):
            if row and str(row[0]).strip().upper() == "NON-VIABLE":
                divider_row = i
                break
        
        if divider_row:
            insert_row = divider_row
        else:
            insert_row = len(rows) + 1
        
        # Build values array based on header
        row_values = []
        for col_name in header:
            key = col_name.strip().lower()
            value = values_by_header.get(key, "")
            row_values.append(value)
        
        # Insert the row
        sheet_id = _first_sheet_props(sheets, spreadsheet_id)[0]
        
        insert_request = {
            "requests": [{
                "insertRange": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": insert_row - 1,
                        "endRowIndex": insert_row
                    },
                    "shiftDimension": "ROWS"
                }
            }]
        }
        
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=insert_request
        ).execute()
        
        # Fill the new row with values
        if row_values:
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_title}!{insert_row}:{insert_row}",
                valueInputOption="RAW",
                body={"values": [row_values]}
            ).execute()
        
        print(f"✨ Inserted new property row {insert_row} above divider")
        return insert_row
        
    except Exception as e:
        print(f"❌ Failed to insert property row: {e}")
        raise

# --- NEW: Row anchoring helpers ---

def get_row_anchor(rowvals: list[str], header: list[str]) -> str:
    """Create a brief row anchor from property address and city."""
    try:
        idx_map = _header_index_map(header)
        
        # Try to find address and city
        addr_keys = ["property address", "address", "street address", "property"]
        city_keys = ["city", "town", "municipality"]
        
        def _get_val(keys: list[str]) -> str:
            for k in keys:
                if k in idx_map:
                    i = idx_map[k] - 1  # 0-based for rowvals
                    if 0 <= i < len(rowvals):
                        v = (rowvals[i] or "").strip()
                        if v:
                            return v
            return ""
        
        addr = _get_val(addr_keys)
        city = _get_val(city_keys)
        
        if addr and city:
            return f"{addr}, {city}"
        elif addr:
            return addr
        elif city:
            return city
        else:
            return f"Row data incomplete"
    except Exception:
        return "Unknown property"

def check_missing_required_fields(rowvals: list[str], header: list[str]) -> list[str]:
    """Check which required fields are missing from the row."""
    try:
        idx_map = _header_index_map(header)
        missing = []
        
        for field in REQUIRED_FIELDS_FOR_CLOSE:
            key = field.strip().lower()
            if key in idx_map:
                i = idx_map[key] - 1  # 0-based
                if i >= len(rowvals) or not (rowvals[i] or "").strip():
                    missing.append(field)
            else:
                missing.append(field)  # Column doesn't exist
        
        return missing
    except Exception as e:
        print(f"❌ Failed to check missing fields: {e}")
        return REQUIRED_FIELDS_FOR_CLOSE  # Assume all missing on error

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

# --- NEW: AI_META helpers ---

def _ensure_ai_meta_tab(sheets, spreadsheet_id: str) -> None:
    """Ensure AI_META tab exists with proper headers."""
    try:
        meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_names = [sheet["properties"]["title"] for sheet in meta["sheets"]]
        
        if "AI_META" in sheet_names:
            return
        
        # Create AI_META tab
        request = {
            "requests": [{
                "addSheet": {
                    "properties": {
                        "title": "AI_META",
                        "hidden": True  # Hidden tab
                    }
                }
            }]
        }
        sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=request).execute()
        
        # Add headers
        headers = ["rowNumber", "columnName", "last_ai_value", "last_ai_write_iso", "human_override"]
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="AI_META!A1:E1",
            valueInputOption="RAW",
            body={"values": [headers]}
        ).execute()
        
        print("📋 Created 'AI_META' tab")
        
    except Exception as e:
        print(f"⚠️ Could not create AI_META tab: {e}")

def _read_ai_meta_row(sheets, spreadsheet_id: str, rownum: int, column: str) -> dict | None:
    """Read AI_META record for specific row/column."""
    try:
        _ensure_ai_meta_tab(sheets, spreadsheet_id)
        
        # Read all AI_META data
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range="AI_META!A:E"
        ).execute()
        
        rows = resp.get("values", [])
        if len(rows) <= 1:  # Only header or empty
            return None
        
        # Find matching row
        for row in rows[1:]:  # Skip header
            if len(row) >= 2 and str(row[0]) == str(rownum) and row[1].lower() == column.lower():
                return {
                    "rowNumber": row[0],
                    "columnName": row[1],
                    "last_ai_value": row[2] if len(row) > 2 else None,
                    "last_ai_write_iso": row[3] if len(row) > 3 else None,
                    "human_override": row[4] if len(row) > 4 else False
                }
        
        return None
        
    except Exception as e:
        print(f"⚠️ Failed to read AI_META for row {rownum}, column {column}: {e}")
        return None

def _append_ai_meta(sheets, spreadsheet_id: str, rownum: int, column: str, value: str, override: bool = False):
    """Append new AI_META record."""
    try:
        _ensure_ai_meta_tab(sheets, spreadsheet_id)
        
        now_iso = datetime.now(timezone.utc).isoformat()
        
        row_data = [rownum, column, value, now_iso, override]
        
        sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="AI_META!A:E",
            valueInputOption="RAW",
            body={"values": [row_data]}
        ).execute()
        
    except Exception as e:
        print(f"⚠️ Failed to append AI_META record: {e}")

# --- NEW: PDF helpers ---

def fetch_pdf_attachments(headers: Dict[str, str], graph_msg_id: str) -> List[Dict[str, Any]]:
    """Fetch PDF attachments from current message only."""
    try:
        base = "https://graph.microsoft.com/v1.0"
        
        # Get attachments
        resp = requests.get(
            f"{base}/me/messages/{graph_msg_id}/attachments",
            headers=headers,
            timeout=30
        )
        resp.raise_for_status()
        
        attachments = resp.json().get("value", [])
        pdf_attachments = []
        
        for attachment in attachments:
            if attachment.get("contentType", "").lower() == "application/pdf":
                name = attachment.get("name", "document.pdf")
                content_bytes = base64.b64decode(attachment.get("contentBytes", ""))
                pdf_attachments.append({
                    "name": name,
                    "bytes": content_bytes
                })
        
        print(f"📎 Found {len(pdf_attachments)} PDF attachment(s)")
        return pdf_attachments
        
    except Exception as e:
        print(f"❌ Failed to fetch PDF attachments: {e}")
        return []

def ensure_drive_folder():
    """Ensure Drive folder exists and return folder ID."""
    try:
        creds = _helper_google_creds()
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        
        # Search for existing folder
        results = drive.files().list(
            q="name='Email PDFs' and mimeType='application/vnd.google-apps.folder'",
            spaces="drive"
        ).execute()
        
        folders = results.get("files", [])
        if folders:
            return folders[0]["id"]
        
        # Create folder
        folder_metadata = {
            "name": "Email PDFs",
            "mimeType": "application/vnd.google-apps.folder"
        }
        
        folder = drive.files().create(body=folder_metadata).execute()
        print(f"📁 Created Drive folder: {folder.get('id')}")
        return folder.get("id")
        
    except Exception as e:
        print(f"❌ Failed to ensure Drive folder: {e}")
        return None

def upload_pdf_to_drive(name: str, content: bytes, folder_id: str = None) -> str | None:
    """Upload PDF to Drive and return webViewLink."""
    try:
        creds = _helper_google_creds()
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        
        if not folder_id:
            folder_id = ensure_drive_folder()
        
        file_metadata = {
            "name": name,
            "parents": [folder_id] if folder_id else []
        }
        
        from googleapiclient.http import MediaIoBaseUpload
        import io
        
        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype="application/pdf",
            resumable=True
        )
        
        file = drive.files().create(
            body=file_metadata,
            media_body=media,
            fields="id,webViewLink"
        ).execute()
        
        # Make link-shareable
        drive.permissions().create(
            fileId=file.get("id"),
            body={
                "role": "reader",
                "type": "anyone"
            }
        ).execute()
        
        web_link = file.get("webViewLink")
        print(f"📁 Uploaded to Drive: {name} -> {web_link}")
        return web_link
        
    except Exception as e:
        print(f"❌ Failed to upload PDF to Drive: {e}")
        return None

def upload_pdf_user_data(filename: str, content: bytes) -> str:
    """Upload PDF to OpenAI with purpose='user_data' and return file_id."""
    try:
        import tempfile
        
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            
            with open(tmp_file.name, "rb") as f:
                file_response = client.files.create(
                    file=f,
                    purpose="user_data"
                )
            
            os.unlink(tmp_file.name)  # Clean up
            
            file_id = file_response.id
            print(f"📤 Uploaded to OpenAI: {filename} -> {file_id}")
            return file_id
            
    except Exception as e:
        print(f"❌ Failed to upload PDF to OpenAI: {e}")
        raise

def append_links_to_flyer_link_column(sheets, spreadsheet_id: str, header: list[str], rownum: int, links: list[str]):
    """Find/create Flyer / Link column and append unique links (no duplicates)."""
    try:
        tab_title = _get_first_tab_title(sheets, spreadsheet_id)
        idx_map = _header_index_map(header)

        # Find 'Flyer / Link' (case-insensitive, trimmed)
        target_key = "flyer / link"
        col_idx = None
        for key, idx in idx_map.items():
            if key == target_key:
                col_idx = idx
                break

        # Create column if missing
        if col_idx is None:
            col_idx = len(header) + 1  # add at end
            col_letter = _col_letter(col_idx)
            sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{tab_title}!{col_letter}2",
                valueInputOption="RAW",
                body={"values": [["Flyer / Link"]]}
            ).execute()
            print(f"📋 Created 'Flyer / Link' column at {col_letter}")
            # (Optional) you may refresh header outside this function if you rely on it elsewhere

        # Cell range for this row/column
        col_letter = _col_letter(col_idx)
        cell_range = f"{tab_title}!{col_letter}{rownum}"

        # Current cell value
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=cell_range
        ).execute()
        current_value = ""
        values = resp.get("values", [])
        if values and values[0]:
            current_value = values[0][0]

        # Existing links (normalized by stripping whitespace)
        existing_lines = [l.strip() for l in (current_value.splitlines() if current_value else []) if l.strip()]
        existing = set(existing_lines)

        # Clean + dedupe incoming links
        additions = []
        for raw in links or []:
            if not raw:
                continue
            clean = _sanitize_url(raw).strip()
            if not clean:
                continue
            if clean not in existing:
                additions.append(clean)
                existing.add(clean)

        if not additions:
            print("ℹ️ All links already present in Flyer / Link")
            return

        # Build updated cell content (preserve prior order, append new)
        updated_lines = existing_lines + additions
        updated_value = "\n".join(updated_lines)

        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[updated_value]]}
        ).execute()

        print(f"🔗 Appended {len(additions)} new link(s) to Flyer / Link")

    except Exception as e:
        print(f"❌ Failed to append links to Flyer / Link column: {e}")


def append_url_to_comments(sheets, spreadsheet_id: str, header: list[str], rownum: int, url: str):
    """Always append URL to Listing Brokers Comments column."""
    try:
        tab_title = _get_first_tab_title(sheets, spreadsheet_id)
        idx_map = _header_index_map(header)
        
        # Look for Listing Brokers Comments column
        target_key = "listing brokers comments"
        col_idx = None
        
        for key, idx in idx_map.items():
            if key == target_key:
                col_idx = idx
                break
        
        if col_idx is None:
            print(f"⚠️ 'Listing Brokers Comments' column not found")
            return
        
        # Get current value
        col_letter = _col_letter(col_idx)
        cell_range = f"{tab_title}!{col_letter}{rownum}"
        
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=cell_range
        ).execute()
        
        current_value = ""
        values = resp.get("values", [])
        if values and values[0]:
            current_value = values[0][0]
        
        # Append URL
        if current_value.strip():
            updated_value = current_value + " • " + url
        else:
            updated_value = url
        
        sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[updated_value]]}
        ).execute()
        
        print(f"🔗 Appended URL to Listing Brokers Comments: {url}")
        
    except Exception as e:
        print(f"❌ Failed to append URL to comments: {e}")

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
    Applies proposal['updates'] to the sheet row with AI write guards.
    Returns {"applied":[...], "skipped":[...]} items with old/new values.
    """
    try:
        sheets = _sheets_client()
        tab_title = _get_first_tab_title(sheets, sheet_id)
        
        _ensure_ai_meta_tab(sheets, sheet_id)

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

            # 1) no-op
            if (old_val or "") == (new_val or ""):
                skipped.append({"column": col_name, "reason": "no-change"})
                continue

            # Check AI_META for write guards
            meta = _read_ai_meta_row(sheets, sheet_id, rownum, col_name)

            # 2) prior AI write and human changed it
            if meta and meta.get("last_ai_value") is not None and str(old_val) != str(meta["last_ai_value"]):
                skipped.append({"column": col_name, "reason": "human-override"})
                continue

            # 3) no prior AI write but cell already has a value → assume human value; skip
            if not meta and (old_val or "").strip() != "":
                skipped.append({"column": col_name, "reason": "existing-human-value"})
                continue

            # 4) otherwise proceed to write...
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

        # Execute batch update
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "valueInputOption": "RAW",
                "data": data_payload
            }
        ).execute()

        # Update AI_META for each applied change
        for a in applied:
            _append_ai_meta(sheets, sheet_id, rownum, a["column"], a["newValue"], override=False)

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

def _find_row_by_anchor(uid: str, thread_id: str, sheets, spreadsheet_id: str, tab_title: str, 
                       header: list[str], fallback_email: str):
    """
    Enhanced row matching: try thread rowNumber first, then fall back to email match.
    """
    try:
        # Check thread metadata for rowNumber
        thread_doc = _fs.collection("users").document(uid).collection("threads").document(thread_id).get()
        if thread_doc.exists:
            thread_data = thread_doc.to_dict() or {}
            stored_row_num = thread_data.get("rowNumber")
            
            if stored_row_num:
                # Verify row exists and get values
                resp = sheets.spreadsheets().values().get(
                    spreadsheetId=spreadsheet_id,
                    range=f"{tab_title}!{stored_row_num}:{stored_row_num}"
                ).execute()
                
                rows = resp.get("values", [])
                if rows and rows[0]:
                    # Pad to header length
                    padded = rows[0] + [""] * (max(0, len(header) - len(rows[0])))
                    print(f"📍 Using thread-anchored row {stored_row_num}")
                    return stored_row_num, padded
        
        # Fall back to email matching
        print(f"📧 Falling back to email matching for {fallback_email}")
        return _find_row_by_email(sheets, spreadsheet_id, tab_title, header, fallback_email)
        
    except Exception as e:
        print(f"⚠️ Row anchor lookup failed: {e}")
        return _find_row_by_email(sheets, spreadsheet_id, tab_title, header, fallback_email)


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


def build_conversation_payload(uid: str, thread_id: str, limit: int = 10) -> list[dict]:
    """
    Return last N messages in chronological order. Each item includes:
    direction, from, to, subject, timestamp, preview (short), content (full text, bounded)
    """
    try:
        messages = _get_thread_messages_chronological(uid, thread_id)
        recent = messages[-limit:] if len(messages) > limit else messages

        payload = []
        CUT = 2000  # cap to keep prompt small but meaningful
        for msg_info in recent:
            data = msg_info["data"]

            ts = data.get("sentDateTime") or data.get("receivedDateTime") or data.get("createdAt")
            if hasattr(ts, "isoformat"):
                ts = ts.isoformat()
            elif not isinstance(ts, str):
                ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            body = data.get("body", {}) or {}
            full_text = (body.get("content") or "")[:CUT]
            preview = (body.get("preview") or "")[:200]

            payload.append({
                "direction": data.get("direction", "unknown"),
                "from": data.get("from", ""),
                "to": data.get("to", []),
                "subject": data.get("subject", ""),
                "timestamp": ts,
                "preview": preview,
                "content": full_text,
            })

        return payload
    except Exception as e:
        print(f"❌ Failed to build conversation payload: {e}")
        return []



def propose_sheet_updates(uid: str, client_id: str, email: str, sheet_id: str, header: list[str],
                          rownum: int, rowvals: list[str], thread_id: str, 
                          file_ids_for_this_run: list[str] = None,
                          url_texts: list[dict] = None) -> dict | None:
    """
    Uses OpenAI Responses API to propose sheet updates.
    Enhanced to support events array and URL exploration text.
    """
    try:
        # Build conversation payload
        conversation = build_conversation_payload(uid, thread_id, limit=10)
        
        # Column rules for money field mapping
        COLUMN_RULES = """
COLUMN SEMANTICS & MAPPING (use EXACT header names):
- "Rent/SF /Yr": Base/asking rent per square foot per YEAR. Synonyms: asking, base rent, $/SF/yr.
- "Ops Ex /SF": NNN/CAM/Operating Expenses per square foot per YEAR. Synonyms: NNN, CAM, OpEx, operating expenses.
- "Gross Rent": If BOTH base rent and NNN are present, set to (Rent/SF /Yr + Ops Ex /SF), rounded to 2 decimals. Else leave unchanged.
- "Listing Brokers Comments ": Short, non-numeric broker/client notes not covered by other columns. Use terse fragments separated by " • ". Do NOT repeat rent/NNN/SF numbers. Pull only explicit statements from the email/attachments. If a comment already exists in this field only add onto it do not remove the existing one while ensuring you aren't duplicating.

FORMATTING:
- For money/area fields, output plain decimals (no "$", "SF", commas). Examples: "30", "14.29", "2400".
- Prefer explicit statements in the email or attachments over inference.
- Example: "$30.00/SF NNN ($14.29/SF)" → "Rent/SF /Yr" = "30", "Ops Ex /SF" = "14.29", "Gross Rent" = "44.29".
- If any such notes exist, include one update for "Listing Brokers Comments " with a single string like: "Directly across from Gold's Gym • Bathrooms in rear corridor can be incorporated into space"

EVENTS DETECTION:
Detect these event types based on conversation content:
- "call_requested": When someone asks for a call or phone conversation
- "property_unavailable": When current property is no longer available/viable
- "new_property": When a NEW property is mentioned (different from current row)
- "close_conversation": When conversation appears complete with all key info provided

For new_property events, extract: address, city, email (if different), link (if mentioned), notes
"""
        
        # Build prompt for OpenAI
        prompt_parts = [f"""
You are analyzing a conversation thread to suggest updates to a Google Sheet row and detect key events.

{COLUMN_RULES}

SHEET HEADER (row 2):
{json.dumps(header)}

CURRENT ROW VALUES (row {rownum}):
{json.dumps(rowvals)}

CONVERSATION HISTORY (latest last):
{json.dumps(conversation, indent=2)}"""]

        # Add URL content if available
        if url_texts:
            prompt_parts.append("\nURL CONTENT FETCHED:")
            for url_info in url_texts:
                prompt_parts.append(f"\nURL: {url_info['url']}")
                prompt_parts.append(f"Content: {url_info['text'][:1000]}...")

        prompt_parts.append("""
Be conservative: only suggest changes you can cite from the text, attachments, or fetched URLs.

OUTPUT ONLY valid JSON in this exact format:
{
  "updates": [
    {
      "column": "<exact header name>",
      "value": "<new value as string>",
      "confidence": 0.85,
      "reason": "<brief explanation why this update is suggested>"
    }
  ],
  "events": [
    {
      "type": "call_requested | property_unavailable | new_property | close_conversation",
      "address": "<for new_property only>",
      "city": "<for new_property only>", 
      "email": "<for new_property if different>",
      "link": "<for new_property if mentioned>",
      "notes": "<for new_property additional context>"
    }
  ],
  "notes": "<optional general notes about the conversation>"
}

Be conservative with updates. Only suggest changes where you have good confidence based on explicit information in the conversation or fetched content.
""")

        prompt = "".join(prompt_parts)

        # Prepare input content for Responses API
        input_content = []
        
        # Add file inputs first
        if file_ids_for_this_run:
            for file_id in file_ids_for_this_run:
                input_content.append({"type": "input_file", "file_id": file_id})
        
        # Add text input
        input_content.append({"type": "input_text", "text": prompt})

        # Call OpenAI Responses API
        response = client.responses.create(
            model=OPENAI_ASSISTANT_MODEL,
            input=[{
                "role": "user",
                "content": input_content
            }]
        )
        
        raw_response = response.output_text.strip()
        
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
            print(f"❌ Failed to parse OpenAI JSON response: {e}")
            print(f"Raw response: {raw_response}")
            return None
        
        # Validate JSON structure
        if not isinstance(proposal, dict):
            print(f"❌ Invalid proposal structure: {proposal}")
            return None
        
        # Ensure updates and events arrays exist
        if "updates" not in proposal:
            proposal["updates"] = []
        if "events" not in proposal:
            proposal["events"] = []
        
        # Log the proposal
        print(f"\n🤖 OpenAI Proposal for {client_id}__{email}:")
        print(json.dumps(proposal, indent=2))
        
        # Store in sheetChangeLog
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()  # ends with +00:00

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
            "fileIds": file_ids_for_this_run or [],
            "urlTexts": url_texts or [],
            "createdAt": SERVER_TIMESTAMP
        }
        
        _fs.collection("users").document(uid).collection("sheetChangeLog").document(log_doc_id).set(change_log_data)
        print(f"💾 Stored proposal in sheetChangeLog/{log_doc_id}")
        
        return proposal
        
    except Exception as e:
        print(f"❌ Failed to propose sheet updates: {e}")
        return None


# --- EXISTING FUNCTIONS (updated to integrate new features) ---

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
        idx_map = _header_index_map(header)  # lowercased key -> 1-based index

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


# --- Send and Index Email ---

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

# --- Enhanced Notifications Function ---

def add_client_notifications(
    uid: str,
    client_id: str,
    email: str,
    thread_id: str,
    applied_updates: list[dict],
    notes: str | None = None,
):
    """
    UPDATED: Writes one notification doc per applied field change.
    Also updates summary on the client doc for quick dashboards.
    """
    try:
        # Write one notification per applied update
        for update in applied_updates:
            dedupe_key = f"{thread_id}:{update.get('range', '')}:{update.get('column', '')}:{update.get('newValue', '')}"
            
            write_notification(
                uid, client_id,
                kind="sheet_update",
                priority="normal",
                email=email,
                thread_id=thread_id,
                row_number=None,  # Could extract from range if needed
                row_anchor=None,
                meta={
                    "column": update.get("column", ""),
                    "oldValue": update.get("oldValue", ""),
                    "newValue": update.get("newValue", ""),
                    "reason": update.get("reason", ""),
                    "confidence": update.get("confidence", 0.0)
                },
                dedupe_key=dedupe_key
            )

        # Legacy summary on client doc
        if applied_updates:
            base_ref = _fs.collection("users").document(uid)
            client_ref = base_ref.collection("clients").document(client_id)
            
            summary_items = [f"{u['column']}='{u['newValue']}'" for u in applied_updates]
            summary = f"Updated {', '.join(summary_items)} for {email}"

            client_ref.set({
                "lastNotificationSummary": summary,
                "lastNotificationAt": SERVER_TIMESTAMP,
            }, merge=True)

            print(f"📢 Created {len(applied_updates)} sheet_update notifications for client {client_id}")

    except Exception as e:
        print(f"❌ Failed to write client notifications: {e}")


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


def _sanitize_url(u: str) -> str:
    if not u:
        return u
    # Trim common trailing junk (punctuation, stray words glued to the URL)
    u = re.sub(r'[\)\]\}\.,;:!?]+$', '', u)
    # If a trailing capitalized token got glued on (e.g., 'Thank'/'Thanks'), drop it
    u = re.sub(r'(?i)(thank(?:s| you)?)$', '', u)
    return u


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
        
        # NEW: Handle PDF attachments for current message only
        file_ids_for_this_run = []
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
                    
                except Exception as e:
                    print(f"❌ Failed to process PDF {pdf['name']}: {e}")
            
            # Append Drive links to Flyer / Link column
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
        
        # NEW: URL exploration - find URLs in message and fetch content
        url_texts = []
        url_pattern = r'https?://[^\s<>"\']+[^\s<>"\'.,;)]'
        urls_found = re.findall(url_pattern, _full_text)
        
        for url in urls_found[:3]:  # Limit to 3 URLs to avoid overwhelming
            clean = _sanitize_url(url)
            fetched_text = fetch_url_as_text(clean)
            if fetched_text:
                url_texts.append({"url": clean, "text": fetched_text})
            
            # Always append URL to Listing Brokers Comments
            try:
                sheets = _sheets_client()
                append_links_to_flyer_link_column(sheets, sheet_id, header, rownum, [clean])
            except Exception as e:
                print(f"❌ Failed to append URL to comments: {e}")
        
        # Step 2: test write
        write_message_order_test(user_id, thread_id, sheet_id)
        
        # Step 3: get proposal using Responses API with URL content
        proposal = propose_sheet_updates(
            user_id, client_id, from_addr_lower, sheet_id, header, rownum, rowvals, 
            thread_id, file_ids_for_this_run, url_texts
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
            
            # NEW: Process events from the proposal
            events = proposal.get("events", [])
            sheets = _sheets_client()
            row_anchor = get_row_anchor(rowvals, header)
            
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
                    except Exception as e:
                        print(f"❌ Failed to write notification: {e}")
                
                elif event_type == "property_unavailable":
                    # Move row below divider and create notification
                    try:
                        tab_title = _get_first_tab_title(sheets, sheet_id)
                        divider_row = ensure_nonviable_divider(sheets, sheet_id, tab_title)
                        new_rownum = move_row_below_divider(sheets, sheet_id, tab_title, rownum, divider_row)
                        
                        # Reformat after move
                        format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                        
                        write_notification(
                            user_id, client_id,
                            kind="property_unavailable",
                            priority="important",
                            email=from_addr_lower,
                            thread_id=thread_id,
                            row_number=new_rownum,
                            row_anchor=row_anchor,
                            meta={"address": event.get("address", ""), "city": event.get("city", "")},
                            dedupe_key=f"property_unavailable:{thread_id}:{rownum}"
                        )
                        
                    except Exception as e:
                        print(f"❌ Failed to handle property_unavailable: {e}")
                
                elif event_type == "new_property":
                    # Insert new property row and start new thread
                    try:
                        address = event.get("address", "")
                        city = event.get("city", "")
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
                        
                        # Put the URL itself in Flyer / Link
                        if link:
                            values_by_header["flyer / link"] = link

                        # Keep human-readable notes (without the URL) in Listing Brokers Comments 
                        if notes:
                            values_by_header["listing brokers comments"] = notes

                        
                        tab_title = _get_first_tab_title(sheets, sheet_id)
                        new_rownum = insert_property_row_above_divider(sheets, sheet_id, tab_title, values_by_header)
                        
                        # Reformat after insert
                        format_sheet_columns_autosize_with_exceptions(sheet_id, header)
                        
                        # Send new property email and get thread ID
                        new_thread_id = send_new_property_email(
                            user_id, client_id, headers, from_addr_lower, address, city, new_rownum
                        )
                        
                        if new_thread_id:
                            write_notification(
                                user_id, client_id,
                                kind="new_property",
                                priority="important",
                                email=from_addr_lower,
                                thread_id=new_thread_id,
                                row_number=new_rownum,
                                row_anchor=f"{address}, {city}" if city else address,
                                meta={"address": address, "city": city, "link": link, "notes": notes},
                                dedupe_key=f"new_property:{address}:{city}"
                            )
                        
                    except Exception as e:
                        print(f"❌ Failed to handle new_property: {e}")
                
                elif event_type == "close_conversation":
                    # Check if all required fields are complete for closing logic
                    pass  # This will be handled below in the required fields check
            
            # NEW: Required fields check and remaining questions flow
            try:
                # Re-read row data in case it was updated
                sheets = _sheets_client()
                tab_title = _get_first_tab_title(sheets, sheet_id)
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

# --- Modified Outbox Processing ---

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

# --- Legacy Functions (kept for compatibility) ---

def send_email(headers, script: str, emails: list[str], client_id: str | None = None):
    """Legacy function - redirects to send_and_index_email"""
    # Note: This legacy function doesn't have user_id, so it can't use the new pipeline
    # Users should migrate to send_and_index_email directly
    raise NotImplementedError("send_email is deprecated. Use send_and_index_email with user_id parameter.")

# --- Utility: List user IDs from Firebase ---
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

# --- Email Functions ---
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
        print("ℹ️ No new replies.")
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

# --- Main Loop ---
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
    print(f"🎯 Using {token_source}; expires_in≈{exp_secs}s – preview: {access_token[:40]}")

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

# --- Entry ---
if __name__ == "__main__":
    all_users = list_user_ids()
    print(f"📦 Found {len(all_users)} token cache users: {all_users}")

    for uid in all_users:
        try:
            refresh_and_process_user(uid)
        except Exception as e:
            print(f"💥 Error for user {uid}:", str(e))