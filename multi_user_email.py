import os
import json
import atexit
import requests
from msal import PublicClientApplication, SerializableTokenCache
import firebase_admin
from firebase_admin import credentials, firestore

# ─── Configuration ──────────────────────────────────────
CLIENT_ID = os.getenv("CLIENT_ID")
if not CLIENT_ID:
    raise RuntimeError("CLIENT_ID environment variable is required.")
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES    = ["Mail.Send", "Mail.ReadWrite"]

# Public API key for Storage REST
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
if not FIREBASE_API_KEY:
    raise RuntimeError("FIREBASE_API_KEY environment variable is required.")

# Storage bucket name
FIREBASE_BUCKET = "email-automation-cache.firebasestorage.app"

# ─── Initialize Firestore Admin ─────────────────────────
sa_key_json = os.getenv("FIREBASE_SA_KEY")
if not sa_key_json:
    raise RuntimeError("FIREBASE_SA_KEY environment variable is required for Firestore access.")
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
    """Download and return the user's cache.json text via Storage REST API."""
    path = f"msal_caches/{uid}/cache.json"
    url = (
        f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o/"
        f"{path.replace('/', '%2F')}?alt=media&key={FIREBASE_API_KEY}"
    )
    r = requests.get(url)
    r.raise_for_status()
    return r.text


def upload_user_cache(uid, content):
    """Upload the given cache content back to Storage as cache.json"""
    path = f"msal_caches/{uid}/cache.json"
    url = (
        f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}/o?uploadType=media"
        f"&name={path}&key={FIREBASE_API_KEY}"
    )
    headers = {"Content-Type": "application/json"}
    r = requests.post(url, headers=headers, data=content)
    r.raise_for_status()


def send_weekly_email(access_token, recipients):
    """Send the weekly questions email to a list of recipients."""
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    SUBJECT = "Weekly Questions"
    BODY = (
        "Hi,\n\nPlease answer the following:\n"
        "1. How was your week?\n"
        "2. What challenges did you face?\n"
        "3. Any updates to share?\n\nThanks!"
    )
    for addr in recipients:
        payload = {
            "message": {"subject": SUBJECT, "body": {"contentType": "Text", "content": BODY},
                        "toRecipients": [{"emailAddress": {"address": addr}}]},
            "saveToSentItems": True
        }
        resp = requests.post("https://graph.microsoft.com/v1.0/me/sendMail", headers=headers, json=payload)
        resp.raise_for_status()
        print(f"✅ Sent weekly email to {addr}")


def process_user(uid):
    """Hydrate MSAL cache, acquire token, and send emails for that user."""
    print(f"--- Processing user: {uid} ---")
    cache_text = download_user_cache(uid)
    cache = SerializableTokenCache()
    cache.deserialize(cache_text)

    # Register save-cache hook
    def _save_cache():
        if cache.has_state_changed:
            new_content = cache.serialize()
            upload_user_cache(uid, new_content)
            print(f"💾 Refreshed cache uploaded for {uid}")
    atexit.register(_save_cache)

    app = PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    # Acquire or refresh token
    accounts = app.get_accounts()
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        result = app.acquire_token_interactive(SCOPES)
    access_token = result.get("access_token")
    if not access_token:
        raise RuntimeError(f"Token acquisition failed: {result}")

    # Send emails for each client
    clients = db.collection("users").document(uid).collection("clients").stream()
    for doc in clients:
        client = doc.to_dict()
        recipients = client.get("emails", [])
        if recipients:
            send_weekly_email(access_token, recipients)


if __name__ == "__main__":
    user_ids = list_user_ids()
    print("User IDs found:", user_ids)
    for uid in user_ids:
        process_user(uid)
