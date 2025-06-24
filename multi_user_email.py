import os
import json
import atexit
import requests
from msal import PublicClientApplication, SerializableTokenCache
import firebase_admin
from firebase_admin import credentials, firestore, storage

# ─── Configuration ──────────────────────────────────────
CLIENT_ID   = os.getenv("CLIENT_ID")
if not CLIENT_ID:
    raise RuntimeError("CLIENT_ID environment variable is required.")
AUTHORITY  = "https://login.microsoftonline.com/common"
SCOPES     = ["Mail.Send", "Mail.ReadWrite"]

# Firebase Storage bucket name (as used in REST URLs)
FIREBASE_STORAGE_BUCKET = os.getenv(
    "FIREBASE_STORAGE_BUCKET",
    "email-automation-cache.firebasestorage.app"
)

# ─── Initialize Firebase Admin SDK ──────────────────────
# Assumes GOOGLE_APPLICATION_CREDENTIALS is set to your service account JSON
cred = credentials.ApplicationDefault()
firebase_admin.initialize_app(cred, {
    'storageBucket': FIREBASE_STORAGE_BUCKET
})

db = firestore.client()
bucket = storage.bucket()

# ─── Helper Functions ───────────────────────────────────
def list_user_ids():
    """List all user IDs in the msal_caches folder (excluding default_user)."""
    blobs = bucket.list_blobs(prefix="msal_caches/")
    uids = set()
    for blob in blobs:
        parts = blob.name.split('/')
        if len(parts) >= 2:
            uid = parts[1]
            if uid and uid != 'default_user':
                uids.add(uid)
    return list(uids)


def download_user_cache(uid):
    """Download and return the cache.json content for a given user_id."""
    blob = bucket.blob(f"msal_caches/{uid}/cache.json")
    data = blob.download_as_text()
    return data


def send_weekly_email(access_token, recipients):
    """Send the weekly questions email to a list of recipients."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
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
        resp = requests.post(
            "https://graph.microsoft.com/v1.0/me/sendMail",
            headers=headers,
            json=payload
        )
        resp.raise_for_status()
        print(f"✅ Sent weekly email to {addr}")


def process_user(uid):
    """Hydrate MSAL cache, acquire token, and send emails for that user."""
    print(f"--- Processing user: {uid} ---")
    cache_json = download_user_cache(uid)
    cache = SerializableTokenCache()
    cache.deserialize(cache_json)

    # Ensure updated cache is re-uploaded if changed
    def _save_cache():
        if cache.has_state_changed:
            new_content = cache.serialize()
            blob = bucket.blob(f"msal_caches/{uid}/cache.json")
            blob.upload_from_string(new_content, content_type="application/json")
            print(f"💾 Refreshed cache uploaded for {uid}")
    atexit.register(_save_cache)

    app = PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
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

    # Fetch all clients for this user
    clients_ref = db.collection("users").document(uid).collection("clients")
    for client_doc in clients_ref.stream():
        client = client_doc.to_dict()
        recipients = client.get("emails", [])
        if recipients:
            send_weekly_email(access_token, recipients)


if __name__ == "__main__":
    user_ids = list_user_ids()
    print("User IDs found:", user_ids)
    for uid in user_ids:
        process_user(uid)
