import os
import json
import atexit
import requests
from msal import PublicClientApplication, SerializableTokenCache

# your existing REST helpers for Storage
from firebase_helpers import download_token, upload_token  

# Firestore Admin SDK
import firebase_admin
from firebase_admin import credentials, firestore

# ─── Configuration ──────────────────────────────────────
CLIENT_ID = os.getenv("CLIENT_ID")
if not CLIENT_ID:
    raise RuntimeError("CLIENT_ID environment variable is required.")
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES    = ["Mail.Send", "Mail.ReadWrite"]

# ─── Initialize Firestore Admin ─────────────────────────
sa_key_json = os.getenv("FIREBASE_SA_KEY")
if not sa_key_json:
    raise RuntimeError("FIREBASE_SA_KEY environment variable is required.")
sa_key = json.loads(sa_key_json)
cred = credentials.Certificate(sa_key)
firebase_admin.initialize_app(cred)

db = firestore.client()

# ─── Helper Functions ───────────────────────────────────
def list_user_ids():
    """Return all UIDs in Firestore/users (except default_user)."""
    docs = db.collection("users").list_documents()
    return [doc.id for doc in docs if doc.id != "default_user"]

def download_user_cache(uid):
    """Download and return the user's cache.json text via REST helper."""
    # This will write a local file "msal_token_cache.bin"
    download_token(os.getenv("FIREBASE_API_KEY"), output_file="msal_token_cache.bin", user_id=uid)
    with open("msal_token_cache.bin", "r") as f:
        return f.read()

def send_weekly_email(access_token, recipients):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json"
    }
    SUBJECT = "Weekly Questions"
    BODY = (
        "Hi,\n\nPlease answer the following:\n"
        "1. How was your week?\n"
        "2. What challenges did you face?\n"
        "3. Any updates to share?\n\nThanks!"
    )
    for addr in recipients:
        payload = {
            "message": {
                "subject": SUBJECT,
                "body": {"contentType": "Text", "content": BODY},
                "toRecipients": [{"emailAddress": {"address": addr}}]
            },
            "saveToSentItems": True
        }
        resp = requests.post("https://graph.microsoft.com/v1.0/me/sendMail",
                             headers=headers, json=payload)
        resp.raise_for_status()
        print(f"✅ Sent weekly email to {addr}")

def process_user(uid):
    print(f"--- Processing user: {uid} ---")
    cache_json = download_user_cache(uid)
    cache = SerializableTokenCache()
    cache.deserialize(cache_json)

    # Re-upload on exit if MSAL rotated tokens
    def _save_cache():
        if cache.has_state_changed:
            with open("msal_token_cache.bin","w") as f:
                f.write(cache.serialize())
            upload_token(os.getenv("FIREBASE_API_KEY"),
                         input_file="msal_token_cache.bin",
                         user_id=uid)
            print(f"💾 Refreshed cache uploaded for {uid}")
    atexit.register(_save_cache)

    app = PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, token_cache=cache
    )

    # Acquire or refresh token
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        result = app.acquire_token_interactive(SCOPES)
    access_token = result.get("access_token")
    if not access_token:
        raise RuntimeError(f"Token acquisition failed: {result}")

    # Fetch all clients for this user and send emails
    clients = db.collection("users").document(uid).collection("clients").stream()
    for client_doc in clients:
        client    = client_doc.to_dict()
        recipients = client.get("emails", [])
        if recipients:
            send_weekly_email(access_token, recipients)

if __name__ == "__main__":
    user_ids = list_user_ids()
    print("User IDs found:", user_ids)
    for uid in user_ids:
        process_user(uid)
