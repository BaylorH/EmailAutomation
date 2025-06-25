# scheduler_runner.py

import os
import json
import atexit
import requests
from openpyxl import Workbook
from msal import PublicClientApplication, SerializableTokenCache

from firebase_helpers import download_token, upload_token, upload_excel

# ─── Environment Config ─────────────────────────────────
CLIENT_ID        = os.getenv("AZURE_API_APP_ID")
TENANT_ID        = os.getenv("AZURE_TENANT_ID")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

USER_ID         = "default_user"  # Could be made dynamic per user

AUTHORITY       = f"https://login.microsoftonline.com/{TENANT_ID}"
SCOPES          = ["Mail.ReadWrite", "Mail.Send"]
TOKEN_CACHE     = "msal_token_cache.bin"
EXCEL_FILE      = "responses.xlsx"

SUBJECT         = "Weekly Questions"
BODY            = (
    "Hi,\n\nPlease answer the following:\n"
    "1. How was your week?\n"
    "2. What challenges did you face?\n"
    "3. Any updates to share?\n\nThanks!"
)
THANK_YOU_BODY  = "Thanks for your response."

if not CLIENT_ID or not TENANT_ID or not FIREBASE_API_KEY:
    raise RuntimeError("Missing CLIENT_ID, TENANT_ID, or FIREBASE_API_KEY.")

# ─── Load Token from Firebase ───────────────────────────
download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=USER_ID)

cache = SerializableTokenCache()
with open(TOKEN_CACHE, "r") as f:
    cache.deserialize(f.read())

def _save_cache():
    if cache.has_state_changed:
        with open(TOKEN_CACHE, "w") as f:
            f.write(cache.serialize())
        upload_token(FIREBASE_API_KEY, input_file=TOKEN_CACHE, user_id=USER_ID)

atexit.register(_save_cache)

# ─── Create MSAL App and Acquire Token ──────────────────
app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

accounts = app.get_accounts()
result = None
if accounts:
    result = app.acquire_token_silent(SCOPES, account=accounts[0])

if not result or "access_token" not in result:
    raise RuntimeError("Silent authentication failed or no token available.")

access_token = result["access_token"]
headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json"
}

# ─── Functions: Email Send + Process Replies ─────────────
def send_weekly_email(to_addresses):
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

def process_replies():
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

    wb.save(EXCEL_FILE)
    upload_excel(FIREBASE_API_KEY, input_file=EXCEL_FILE)
    print(f"✅ Saved {len(messages)} replies to {EXCEL_FILE}")

# ─── Main Execution ─────────────────────────────────────
if __name__ == "__main__":
    recipients = ["bp21harrison@gmail.com"]  # You can load this from Firebase or env if needed

    send_weekly_email(recipients)
    process_replies()
