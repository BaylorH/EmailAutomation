from datetime import datetime, timezone
from .clients import _sheets_client, _fs
from .sheets import _get_first_tab_title
from .messaging import _get_thread_messages_chronological

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