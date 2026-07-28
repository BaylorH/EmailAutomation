import os

LEGACY_EMAIL_OPERATIONS_FLAG = "SITESIFT_ENABLE_LEGACY_EMAIL_OPERATIONS"


class LegacyStandaloneEffectsDisabled(RuntimeError):
    """Raised before the historical standalone script can perform any effects."""


def _require_legacy_email_operations_enabled(function_name: str) -> None:
    if os.environ.get(LEGACY_EMAIL_OPERATIONS_FLAG) == "1":
        return
    raise LegacyStandaloneEffectsDisabled(
        f"{function_name} is a legacy direct-Graph helper and is disabled by "
        f"default. Set {LEGACY_EMAIL_OPERATIONS_FLAG}=1 only for a controlled "
        "migration test."
    )


_require_legacy_email_operations_enabled("module_import")

import json
import atexit
import requests
import openpyxl
from openpyxl import Workbook
from msal import PublicClientApplication, SerializableTokenCache

from firebase_helpers import download_token, upload_token, upload_excel

# ─── Configuration ──────────────────────────────────────
AUTHORITY      = "https://login.microsoftonline.com/common"
SCOPES         = ["Mail.Send", "Mail.ReadWrite"]  # 'offline_access' is reserved and added automatically
TOKEN_CACHE    = "msal_token_cache.bin"
EXCEL_FILE     = "responses.xlsx"
FIREBASE_API_KEY = None
CLIENT_ID = None
cache = None
app = None
headers = {}

SUBJECT        = "Weekly Questions"
BODY           = (
    "Hi,\n\nPlease answer the following:\n"
    "1. How was your week?\n"
    "2. What challenges did you face?\n"
    "3. Any updates to share?\n\nThanks!"
)
THANK_YOU_BODY = "Thanks for your response."

def _save_cache():
    if cache is not None and cache.has_state_changed:
        with open("msal_token_cache.bin", "w") as f:
            f.write(cache.serialize())
        upload_token(FIREBASE_API_KEY, input_file="msal_token_cache.bin", user_id="default_user")


def _bootstrap_runtime():
    """Perform the historical token/bootstrap effects only from guarded main."""

    _require_legacy_email_operations_enabled("bootstrap_runtime")
    global FIREBASE_API_KEY, CLIENT_ID, cache, app, headers

    FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
    if not FIREBASE_API_KEY:
        raise RuntimeError("FIREBASE_API_KEY is not set in the environment.")
    CLIENT_ID = os.getenv("CLIENT_ID")
    if not CLIENT_ID:
        raise RuntimeError("Set CLIENT_ID in the environment")

    download_token(FIREBASE_API_KEY)
    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as cache_file:
        cache.deserialize(cache_file.read())
    atexit.register(_save_cache)

    app = PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache,
    )
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        result = app.acquire_token_interactive(SCOPES)

    access_token = result.get("access_token")
    if not access_token:
        raise RuntimeError(
            f"Token acquisition failed: {json.dumps(result, indent=2)}"
        )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

# ─── Send weekly question email ───────────────────────────
def send_weekly_email(to_addresses):
    _require_legacy_email_operations_enabled("send_weekly_email")
    for addr in to_addresses:
        payload = {
            "message": {
                "subject": SUBJECT,
                "body": {"contentType": "Text", "content": BODY},
                "toRecipients": [{"emailAddress": {"address": addr}}]
            },
            "saveToSentItems": True
        }
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers=headers,
            json=payload
        )
        resp.raise_for_status()
        print(f"✅ Sent '{SUBJECT}' to {addr}")

# ─── Thank repliers and log replies to Excel ──────────────
def process_replies():
    _require_legacy_email_operations_enabled("process_replies")
    # Fetch unread replies - try multiple filter approaches
    url = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
    
    # Option 1: Try with startswith instead of contains
    params = {
        '$filter': f"isRead eq false and startswith(subject,'Re: {SUBJECT}')",
        '$top': '10',
        '$orderby': 'receivedDateTime desc'
    }
    
    try:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        print(f"First filter attempt failed: {e}")
        # Option 2: Try a simpler filter
        params = {
            '$filter': "isRead eq false",
            '$top': '50',  # Get more messages to filter manually
            '$orderby': 'receivedDateTime desc'
        }
        try:
            resp = requests.get(url, headers=headers, params=params)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e2:
            print(f"Second filter attempt failed: {e2}")
            # Option 3: Get all unread messages without subject filter
            params = {
                '$filter': "isRead eq false",
                '$top': '50'
            }
            resp = requests.get(url, headers=headers, params=params)
            resp.raise_for_status()
    
    messages = resp.json().get("value", [])
    
    # Filter messages manually if server-side filtering failed
    if '$filter' in str(params) and 'subject' not in params['$filter']:
        # Manually filter for replies to our subject
        filtered_messages = []
        for msg in messages:
            subject = msg.get("subject", "").lower()
            if f"re: {SUBJECT.lower()}" in subject or f"reply: {SUBJECT.lower()}" in subject:
                filtered_messages.append(msg)
        messages = filtered_messages
    
    if not messages:
        print("ℹ️  No new replies.")
        return

    # Create a new workbook every time
    wb = Workbook()
    ws = wb.active
    ws.append(["Sender", "Response", "ReceivedDateTime"])  # Header row

    for msg in messages:
        sender = msg["from"]["emailAddress"]["address"]
        body   = msg["body"]["content"].strip()
        dt     = msg["receivedDateTime"]

        # Send a thank-you reply
        reply_url = f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}/reply"
        reply_payload = {"message": {"body": {"contentType": "Text", "content": THANK_YOU_BODY}}}
        r = requests.post(reply_url, headers=headers, json=reply_payload)
        r.raise_for_status()

        # Mark original as read
        mark_read_url = f"https://graph.microsoft.com/v1.0/me/messages/{msg['id']}"
        requests.patch(mark_read_url, headers=headers, json={"isRead": True})

        # Log to Excel
        ws.append([sender, body, dt])
        print(f"📥 Replied to and logged reply from {sender}")

    wb.save(EXCEL_FILE)
    upload_excel(FIREBASE_API_KEY, input_file=EXCEL_FILE)
    print(f"✅ Saved {len(messages)} replies to {EXCEL_FILE}")

# ─── Debug function to test API calls ─────────────────────
def debug_api():
    """Test basic API connectivity and permissions"""
    try:
        # Test basic inbox access
        url = "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages"
        params = {'$top': '1'}
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        print("✅ Basic inbox access works")
        
        # Test unread filter
        params = {'$filter': 'isRead eq false', '$top': '1'}
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        print("✅ Unread filter works")
        
        # Test subject contains filter
        params = {'$filter': "contains(subject,'Weekly')", '$top': '1'}
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        print("✅ Subject contains filter works")
        
    except requests.exceptions.HTTPError as e:
        print(f"❌ API test failed: {e}")
        print(f"Response: {e.response.text if e.response else 'No response'}")

if __name__ == "__main__":
    _bootstrap_runtime()
    # Replace or extend this list as needed
    recipients = ["bp21harrison@gmail.com"]

    send_weekly_email(recipients)
    
    # Uncomment the next line to debug API issues
    # debug_api()
    
    process_replies()
