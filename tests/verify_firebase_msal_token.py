#!/usr/bin/env python3
"""
Test script to verify Firebase Functions-created MSAL tokens work with Python MSAL.
This verifies the token format compatibility fix in msalCallback.
"""

import os
import sys
import json
import tempfile
import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import firebase_admin
from firebase_admin import credentials, storage
from msal import ConfidentialClientApplication

# Configuration
USER_ID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"  # baylor.freelance@outlook.com
CLIENT_ID = os.environ.get("AZURE_API_APP_ID")
CLIENT_SECRET = os.environ.get("AZURE_API_CLIENT_SECRET")
AUTHORITY = f"https://login.microsoftonline.com/{os.environ.get('AZURE_TENANT_ID', 'common')}"
SCOPES = ["Mail.ReadWrite", "Mail.Send"]


def init_firebase():
    """Initialize Firebase Admin SDK."""
    try:
        cred = credentials.Certificate("service-account.json")
        firebase_admin.initialize_app(cred, {
            'storageBucket': 'email-automation-cache.firebasestorage.app'
        })
    except ValueError:
        pass  # Already initialized


def download_token_from_firebase() -> str:
    """Download MSAL token cache from Firebase Storage."""
    bucket = storage.bucket()
    blob = bucket.blob(f"msal_caches/{USER_ID}/msal_token_cache.bin")

    if not blob.exists():
        print("❌ No MSAL token found in Firebase Storage")
        print(f"   Path: msal_caches/{USER_ID}/msal_token_cache.bin")
        print("\n   Please re-authenticate at:")
        print("   https://email-automation-cache.web.app/test-msal")
        return None

    # Download to temp file
    with tempfile.NamedTemporaryFile(mode='wb', suffix='.bin', delete=False) as f:
        blob.download_to_file(f)
        return f.name


def test_token_format(cache_path: str):
    """Verify the token cache has Python-compatible format."""
    print("\n📋 Checking token cache format...")

    with open(cache_path, 'r') as f:
        cache_data = json.load(f)

    # Check required sections
    required_sections = ['AccessToken', 'Account', 'RefreshToken']
    for section in required_sections:
        if section not in cache_data:
            print(f"❌ Missing section: {section}")
            return False
        if not cache_data[section]:
            print(f"⚠️  Empty section: {section}")

    # Check RefreshToken has 'target' field (the key fix!)
    refresh_tokens = cache_data.get('RefreshToken', {})
    if refresh_tokens:
        for key, rt in refresh_tokens.items():
            if 'target' not in rt:
                print(f"❌ RefreshToken missing 'target' field!")
                print(f"   Key: {key}")
                print(f"   Fields: {list(rt.keys())}")
                return False
            print(f"✅ RefreshToken has 'target' field: {rt['target'][:50]}...")

    print("✅ Token cache format looks correct")
    return True


def test_acquire_token_silent(cache_path: str):
    """Test that Python MSAL can use the token with acquire_token_silent."""
    print("\n🔐 Testing acquire_token_silent...")

    # Load cache
    with open(cache_path, 'r') as f:
        cache_data = f.read()

    # Create MSAL app with cache
    from msal import SerializableTokenCache
    cache = SerializableTokenCache()
    cache.deserialize(cache_data)

    app = ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
        token_cache=cache
    )

    # Get accounts
    accounts = app.get_accounts()
    if not accounts:
        print("❌ No accounts found in token cache")
        return None

    account = accounts[0]
    print(f"   Found account: {account.get('username', 'unknown')}")

    # Try to acquire token silently
    result = app.acquire_token_silent(SCOPES, account=account)

    if result and 'access_token' in result:
        print("✅ acquire_token_silent succeeded!")
        return result['access_token']
    else:
        error = result.get('error_description', 'Unknown error') if result else 'No result'
        print(f"❌ acquire_token_silent failed: {error}")
        return None


def test_graph_api_call(access_token: str):
    """Test making an actual Microsoft Graph API call."""
    print("\n📧 Testing Microsoft Graph API call...")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Get mail folders (works for both consumer and enterprise accounts)
    # Note: /me endpoint may fail for consumer Outlook accounts, so we test mailFolders
    response = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders",
        headers=headers,
        params={"$top": "5"}
    )

    if response.status_code == 200:
        data = response.json()
        folders = data.get('value', [])
        print(f"✅ Graph API call succeeded!")
        print(f"   Found {len(folders)} mail folders")
        for folder in folders[:3]:
            print(f"   - {folder.get('displayName')}")
        return True
    else:
        print(f"❌ Graph API call failed: {response.status_code}")
        print(f"   {response.text[:200]}")
        return False


def test_mailbox_access(access_token: str):
    """Test accessing the mailbox (the actual use case)."""
    print("\n📬 Testing mailbox access...")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Get recent inbox messages
    response = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
        headers=headers,
        params={"$top": "5", "$select": "subject,from,receivedDateTime"}
    )

    if response.status_code == 200:
        data = response.json()
        messages = data.get('value', [])
        print(f"✅ Mailbox access succeeded!")
        print(f"   Found {len(messages)} recent messages")
        for msg in messages[:3]:
            sender = msg.get('from', {}).get('emailAddress', {}).get('address', 'unknown')
            subject = msg.get('subject', 'No subject')[:40]
            print(f"   - {sender}: {subject}...")
        return True
    else:
        print(f"❌ Mailbox access failed: {response.status_code}")
        print(f"   {response.text[:200]}")
        return False


def main():
    print("=" * 60)
    print("Firebase MSAL Token Compatibility Test")
    print("=" * 60)
    print(f"\nTesting user: {USER_ID} (baylor.freelance@outlook.com)")

    # Initialize Firebase
    init_firebase()

    # Step 1: Download token
    print("\n📥 Downloading token from Firebase Storage...")
    cache_path = download_token_from_firebase()
    if not cache_path:
        sys.exit(1)
    print(f"   Downloaded to: {cache_path}")

    # Step 2: Check format
    if not test_token_format(cache_path):
        print("\n❌ FAILED: Token format is not Python-compatible")
        sys.exit(1)

    # Step 3: Test acquire_token_silent
    access_token = test_acquire_token_silent(cache_path)
    if not access_token:
        print("\n❌ FAILED: Could not acquire token silently")
        sys.exit(1)

    # Step 4: Test Graph API
    if not test_graph_api_call(access_token):
        print("\n❌ FAILED: Graph API call failed")
        sys.exit(1)

    # Step 5: Test mailbox
    if not test_mailbox_access(access_token):
        print("\n❌ FAILED: Mailbox access failed")
        sys.exit(1)

    # Cleanup
    os.unlink(cache_path)

    print("\n" + "=" * 60)
    print("✅ ALL TESTS PASSED!")
    print("=" * 60)
    print("\nFirebase Functions MSAL tokens are fully compatible with Python backend.")
    print("The Render server can be safely replaced with Firebase Functions.")


if __name__ == "__main__":
    main()
