import os
import json
import requests
from msal import SerializableTokenCache, PublicClientApplication
import firebase_admin
from firebase_admin import credentials, storage
from datetime import datetime

# ─── Configuration ──────────────────────────────────────
CLIENT_ID = os.getenv("CLIENT_ID")
if not CLIENT_ID:
    raise RuntimeError("CLIENT_ID environment variable is required.")

AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["https://graph.microsoft.com/Mail.Send", "https://graph.microsoft.com/Mail.ReadWrite"]

# Firebase configuration
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY")
FIREBASE_BUCKET = "email-automation-cache.firebasestorage.app"

# ─── Initialize Firebase Admin ─────────────────────────
sa_key_json = os.getenv("FIREBASE_SA_KEY")
if not sa_key_json:
    raise RuntimeError("FIREBASE_SA_KEY environment variable is required.")

sa_key = json.loads(sa_key_json)
cred = credentials.Certificate(sa_key)
firebase_admin.initialize_app(cred, {
    'storageBucket': FIREBASE_BUCKET
})

from firebase_admin import firestore
db = firestore.client()

# ─── Helper Functions ───────────────────────────────────
def download_user_cache(uid):
    """Download MSAL cache from Firebase Storage."""
    try:
        bucket = storage.bucket()
        blob = bucket.blob(f'msal_caches/{uid}/cache.json')
        
        if not blob.exists():
            raise RuntimeError(f"No MSAL cache found for user {uid}")
        
        cache_data = blob.download_as_text()
        print(f"✅ Downloaded cache for user {uid}")
        return cache_data
    except Exception as e:
        print(f"❌ Error downloading cache for {uid}: {e}")
        raise


def upload_user_cache(uid, cache_content):
    """Upload updated MSAL cache to Firebase Storage."""
    try:
        bucket = storage.bucket()
        blob = bucket.blob(f'msal_caches/{uid}/cache.json')
        blob.upload_from_string(cache_content, content_type='application/json')
        print(f"✅ Uploaded updated cache for user {uid}")
    except Exception as e:
        print(f"❌ Error uploading cache for {uid}: {e}")
        raise


def convert_browser_cache_to_msal(browser_cache_data):
    """
    Convert the browser MSAL cache format to the format expected by Python MSAL.
    The browser cache is already in the right format, we just need to serialize it properly.
    """
    if isinstance(browser_cache_data, str):
        browser_cache_data = json.loads(browser_cache_data)
    
    # The browser cache is already in MSAL SerializableTokenCache format
    # We just need to return it as a JSON string
    return json.dumps(browser_cache_data)


def get_access_token_for_user(uid):
    """
    Get a valid access token for the user, refreshing if necessary.
    """
    print(f"🔄 Getting access token for user {uid}")
    
    # Download the MSAL cache
    cache_data = download_user_cache(uid)
    
    # Create a SerializableTokenCache and load the data
    cache = SerializableTokenCache()
    cache.deserialize(cache_data)
    
    # Create MSAL PublicClientApplication
    app = PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
    )
    
    # Try to get accounts from cache
    accounts = app.get_accounts()
    print(f"Found {len(accounts)} accounts in cache")
    
    if not accounts:
        raise RuntimeError(f"No accounts found in cache for user {uid}")
    
    # Use the first account (assuming single user)
    account = accounts[0]
    print(f"Using account: {account.get('username', 'unknown')}")
    
    # Try silent token acquisition first
    result = app.acquire_token_silent(SCOPES, account=account)
    
    if result and "access_token" in result:
        print("✅ Successfully acquired token silently")
        
        # Save updated cache if it changed
        if cache.has_state_changed:
            print("💾 Cache has changed, uploading updated cache")
            upload_user_cache(uid, cache.serialize())
        
        return result["access_token"]
    else:
        print("❌ Silent token acquisition failed")
        if result:
            print(f"Error: {result.get('error', 'Unknown error')}")
            print(f"Error description: {result.get('error_description', 'No description')}")
        
        # For a headless server, we can't do interactive auth
        # The user would need to re-authenticate in the browser
        raise RuntimeError(f"Token refresh failed for user {uid}. User may need to re-authenticate.")


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
                "body": {
                    "contentType": "Text",
                    "content": BODY
                },
                "toRecipients": [
                    {"emailAddress": {"address": addr}}
                ]
            },
            "saveToSentItems": True
        }
        
        try:
            resp = requests.post(
                "https://graph.microsoft.com/v1.0/me/sendMail",
                headers=headers,
                json=payload
            )
            resp.raise_for_status()
            print(f"✅ Sent weekly email to {addr}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                print(f"❌ Unauthorized: Token expired or invalid")
                raise
            else:
                print(f"❌ Failed to send email to {addr}: {e}")
                if e.response.text:
                    print(f"Response: {e.response.text}")
                raise


def list_user_ids():
    """Return all UIDs that have MSAL caches."""
    try:
        bucket = storage.bucket()
        blobs = bucket.list_blobs(prefix='msal_caches/')
        
        user_ids = set()
        for blob in blobs:
            # Extract UID from path like 'msal_caches/USER_ID/cache.json'
            path_parts = blob.name.split('/')
            if len(path_parts) >= 2 and path_parts[0] == 'msal_caches':
                user_ids.add(path_parts[1])
        
        return list(user_ids)
    except Exception as e:
        print(f"❌ Error listing user IDs: {e}")
        return []


def process_user(uid):
    """Process emails for a specific user."""
    print(f"\n--- Processing user: {uid} ---")
    
    try:
        # Get access token (will refresh if needed)
        access_token = get_access_token_for_user(uid)
        
        # Get clients for this user from Firestore
        clients_ref = db.collection("users").document(uid).collection("clients")
        clients = clients_ref.stream()
        
        client_count = 0
        for client_doc in clients:
            client_count += 1
            client = client_doc.to_dict()
            recipients = client.get("emails", [])
            
            if recipients:
                print(f"📧 Sending emails for client: {client_doc.id}")
                send_weekly_email(access_token, recipients)
            else:
                print(f"⚠️  No email recipients for client: {client_doc.id}")
        
        if client_count == 0:
            print(f"⚠️  No clients found for user {uid}")
            
    except Exception as e:
        print(f"❌ Error processing user {uid}: {e}")
        raise


if __name__ == "__main__":
    print("🚀 Starting email automation script")
    
    # List all users with MSAL caches
    user_ids = list_user_ids()
    print(f"📋 Found {len(user_ids)} users with MSAL caches: {user_ids}")
    
    if not user_ids:
        print("❌ No users found with MSAL caches")
        exit(1)
    
    # Process each user
    for uid in user_ids:
        try:
            process_user(uid)
        except Exception as e:
            print(f"❌ Failed to process user {uid}: {e}")
            continue
    
    print("✅ Email automation script completed")
