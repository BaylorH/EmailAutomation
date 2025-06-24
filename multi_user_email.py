import os
import json
import requests
from msal import SerializableTokenCache, PublicClientApplication
import firebase_admin
from firebase_admin import credentials, storage
from datetime import datetime
import base64
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        logger.info(f"✅ Downloaded cache for user {uid}")
        return cache_data
    except Exception as e:
        logger.error(f"❌ Error downloading cache for {uid}: {e}")
        raise


def upload_user_cache(uid, cache_content):
    """Upload updated MSAL cache to Firebase Storage."""
    try:
        bucket = storage.bucket()
        blob = bucket.blob(f'msal_caches/{uid}/cache.json')
        blob.upload_from_string(cache_content, content_type='application/json')
        logger.info(f"✅ Uploaded updated cache for user {uid}")
    except Exception as e:
        logger.error(f"❌ Error uploading cache for {uid}: {e}")
        raise


def extract_token_from_browser_cache(browser_cache_data):
    """
    Extract usable token information from browser MSAL cache.
    Browser cache stores encrypted tokens that we need to decrypt or extract.
    """
    try:
        if isinstance(browser_cache_data, str):
            browser_cache_data = json.loads(browser_cache_data)
        
        # Look for account information
        accounts = {}
        access_tokens = {}
        refresh_tokens = {}
        id_tokens = {}
        
        for key, value in browser_cache_data.items():
            if key == "msal.account.keys":
                # Extract account keys
                for account_key in value:
                    if account_key in browser_cache_data:
                        accounts[account_key] = browser_cache_data[account_key]
            elif "accesstoken" in key.lower() and key.startswith("00000000"):
                access_tokens[key] = value
            elif "refreshtoken" in key.lower() and key.startswith("00000000"):
                refresh_tokens[key] = value
            elif "idtoken" in key.lower() and key.startswith("00000000"):
                id_tokens[key] = value
        
        logger.info(f"Found {len(accounts)} accounts, {len(access_tokens)} access tokens, {len(refresh_tokens)} refresh tokens")
        
        # For now, let's try to use the refresh token to get a new access token
        # This is a simplified approach - in production you'd want to properly decrypt the browser cache
        return {
            "accounts": accounts,
            "access_tokens": access_tokens,
            "refresh_tokens": refresh_tokens,
            "id_tokens": id_tokens
        }
        
    except Exception as e:
        logger.error(f"Error extracting token from browser cache: {e}")
        raise


def get_access_token_via_refresh_token(refresh_token_encrypted):
    """
    Attempt to get a new access token using the refresh token.
    Note: This is a simplified approach since browser tokens are encrypted.
    """
    try:
        # In a real scenario, you'd need to decrypt the browser cache data
        # For now, we'll try to use MSAL to get a new token
        
        # Create a new cache and try to populate it
        cache = SerializableTokenCache()
        
        # Create MSAL PublicClientApplication
        app = PublicClientApplication(
            CLIENT_ID,
            authority=AUTHORITY,
            token_cache=cache
        )
        
        # Since we can't easily decrypt browser tokens, we'll need the user to re-authenticate
        # For a server-side application, you might want to:
        # 1. Store the decrypted tokens properly during initial auth
        # 2. Use a confidential client app with client secrets
        # 3. Implement proper token decryption for browser cache
        
        return None
        
    except Exception as e:
        logger.error(f"Error getting access token via refresh token: {e}")
        return None


def create_python_compatible_cache(browser_cache_data):
    """
    Create a Python MSAL compatible cache from browser cache data.
    This is a workaround since browser and Python MSAL use different encryption.
    """
    try:
        # Extract the raw data
        extracted_data = extract_token_from_browser_cache(browser_cache_data)
        
        # Create a new Python MSAL cache structure
        python_cache = {
            "AccessToken": {},
            "RefreshToken": {},
            "IdToken": {},
            "Account": {},
            "AppMetadata": {}
        }
        
        # Note: This is where you'd normally decrypt and convert the tokens
        # Since browser tokens are encrypted, this is a placeholder
        # In practice, you'd need to either:
        # 1. Have the user re-authenticate through your Python app
        # 2. Use a different approach like storing tokens server-side during initial auth
        # 3. Implement proper browser cache decryption
        
        return json.dumps(python_cache)
        
    except Exception as e:
        logger.error(f"Error creating Python compatible cache: {e}")
        raise


def get_access_token_for_user(uid):
    """
    Get a valid access token for the user, with improved browser cache handling.
    """
    logger.info(f"🔄 Getting access token for user {uid}")
    
    try:
        # Download the browser cache
        cache_data = download_user_cache(uid)
        
        # Try to parse as browser cache first
        browser_cache = json.loads(cache_data)
        
        # Check if this looks like a browser cache (has encrypted data)
        if any("data" in str(value) for value in browser_cache.values() if isinstance(value, dict)):
            logger.warning(f"⚠️  Detected browser cache format for user {uid}")
            logger.warning("Browser cache contains encrypted tokens that cannot be directly used by Python MSAL")
            logger.warning("User needs to re-authenticate through the Python application")
            
            # You could implement a re-authentication flow here
            # For now, we'll skip this user
            raise RuntimeError(f"Browser cache detected for user {uid}. User needs to re-authenticate through Python app.")
        
        # If it's already a Python MSAL cache, proceed normally
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
        logger.info(f"Found {len(accounts)} accounts in cache")
        
        if not accounts:
            raise RuntimeError(f"No accounts found in cache for user {uid}")
        
        # Use the first account
        account = accounts[0]
        logger.info(f"Using account: {account.get('username', 'unknown')}")
        
        # Try silent token acquisition
        result = app.acquire_token_silent(SCOPES, account=account)
        
        if result and "access_token" in result:
            logger.info("✅ Successfully acquired token silently")
            
            # Save updated cache if it changed
            if cache.has_state_changed:
                logger.info("💾 Cache has changed, uploading updated cache")
                upload_user_cache(uid, cache.serialize())
            
            return result["access_token"]
        else:
            logger.error("❌ Silent token acquisition failed")
            if result:
                logger.error(f"Error: {result.get('error', 'Unknown error')}")
                logger.error(f"Error description: {result.get('error_description', 'No description')}")
            
            raise RuntimeError(f"Token refresh failed for user {uid}. User may need to re-authenticate.")
            
    except json.JSONDecodeError:
        # Not a JSON cache, might be binary
        raise RuntimeError(f"Invalid cache format for user {uid}")
    except Exception as e:
        logger.error(f"Error getting access token for user {uid}: {e}")
        raise


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
            logger.info(f"✅ Sent weekly email to {addr}")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                logger.error(f"❌ Unauthorized: Token expired or invalid")
                raise
            else:
                logger.error(f"❌ Failed to send email to {addr}: {e}")
                if e.response.text:
                    logger.error(f"Response: {e.response.text}")
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
        logger.error(f"❌ Error listing user IDs: {e}")
        return []


def process_user(uid):
    """Process emails for a specific user."""
    logger.info(f"\n--- Processing user: {uid} ---")
    
    try:
        # Skip default_user as mentioned
        if uid == "default_user":
            logger.info(f"⏭️  Skipping default_user as requested")
            return
        
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
                logger.info(f"📧 Sending emails for client: {client_doc.id}")
                send_weekly_email(access_token, recipients)
            else:
                logger.warning(f"⚠️  No email recipients for client: {client_doc.id}")
        
        if client_count == 0:
            logger.warning(f"⚠️  No clients found for user {uid}")
            
    except Exception as e:
        logger.error(f"❌ Error processing user {uid}: {e}")
        # Continue with other users instead of raising
        return


def setup_user_reauth_flow(uid):
    """
    Set up a re-authentication flow for users with browser caches.
    This would typically involve creating a web endpoint or device flow.
    """
    logger.info(f"🔄 Setting up re-authentication for user {uid}")
    
    # Create MSAL PublicClientApplication
    app = PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY
    )
    
    # For a server environment, you might use device flow:
    flow = app.initiate_device_flow(scopes=SCOPES)
    
    if "user_code" not in flow:
        raise ValueError("Fail to create device flow")
    
    logger.info(f"📱 Device flow initiated for user {uid}")
    logger.info(f"User code: {flow['user_code']}")
    logger.info(f"Go to: {flow['verification_uri']} and enter the code")
    
    # In a real application, you'd save this flow info and check it periodically
    # or provide a webhook endpoint for completion
    
    return flow


if __name__ == "__main__":
    logger.info("🚀 Starting email automation script")
    
    # List all users with MSAL caches
    user_ids = list_user_ids()
    logger.info(f"📋 Found {len(user_ids)} users with MSAL caches: {user_ids}")
    
    if not user_ids:
        logger.error("❌ No users found with MSAL caches")
        exit(1)
    
    # Process each user
    successful_users = 0
    failed_users = []
    
    for uid in user_ids:
        try:
            process_user(uid)
            successful_users += 1
        except Exception as e:
            logger.error(f"❌ Failed to process user {uid}: {e}")
            failed_users.append(uid)
            continue
    
    logger.info(f"✅ Email automation completed")
    logger.info(f"📊 Successfully processed: {successful_users} users")
    if failed_users:
        logger.warning(f"⚠️  Failed to process: {len(failed_users)} users: {failed_users}")
        logger.info("💡 Users with browser caches need to re-authenticate through the Python application")
