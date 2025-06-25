# scheduler_runner.py

import os
import json
import atexit
import base64
import requests
from openpyxl import Workbook
from msal import PublicClientApplication, SerializableTokenCache

from firebase_helpers import download_token, upload_token, upload_excel

# ─── Environment Config ─────────────────────────────────
CLIENT_ID        = os.getenv("AZURE_API_APP_ID")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")

USER_ID         = "default_user"
AUTHORITY = "https://login.microsoftonline.com/common"
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

if not CLIENT_ID or not FIREBASE_API_KEY:
    raise RuntimeError("Missing CLIENT_ID or FIREBASE_API_KEY.")

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

# ─── Acquire Token ──────────────────────────────────────
app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)
accounts = app.get_accounts()
result = None
if accounts:
    result = app.acquire_token_silent(SCOPES, account=accounts[0])

if not result or "access_token" not in result:
    raise RuntimeError("Silent authentication failed or no token available.")

# 🔍 Check token source
def decode_token_payload(token):
    payload = token.split(".")[1]
    padded = payload + '=' * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))

decoded = decode_token_payload(result["access_token"])
appid = decoded.get("appid", "")

if not appid.startswith("54cec"):
    raise RuntimeError("❌ Token was issued by the wrong Azure app.")
else:
    print("✅ Token appid starts with correct prefix.")

access_token = result["access_token"]
headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json"
}

# ─── DEBUG: Check if mailbox exists ─────────────────────
print("👤 Checking /me info...")
me_resp = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)
print("🔍 /me response:", me_resp.status_code, me_resp.json())

print("\n📬 Checking /me/mailFolders...")
folders_resp = requests.get("https://graph.microsoft.com/v1.0/me/mailFolders", headers=headers)
print("🔍 /me/mailFolders response:", folders_resp.status_code, folders_resp.json())

# ─── Functions ──────────────────────────────────────────
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

# ─── Run ────────────────────────────────────────────────
if __name__ == "__main__":
    recipients = ["bp21harrison@gmail.com"]
    send_weekly_email(recipients)
    process_replies()
