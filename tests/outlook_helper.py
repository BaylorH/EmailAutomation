#!/usr/bin/env python3
"""
Outlook Inbox Helper
====================
Read emails from any configured Outlook account.

Usage:
    python tests/outlook_helper.py inbox [user_id]     # List inbox messages
    python tests/outlook_helper.py attachments <msg_id> [user_id]  # Download attachments
    python tests/outlook_helper.py users               # List available user IDs

Default user_id: xG7jAeu8ceYVBhXQwDFRfvLmvpH2 (baylor.freelance@outlook.com)

IMPORTANT: This is THE canonical way to access Outlook. Do not reinvent this.
"""

import os
import sys
import json
import base64

# Add parent to path
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, parent_dir)

# Set Google credentials BEFORE any imports that need it
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(parent_dir, 'service-account.json')

# Load environment variables from .env file
env_path = os.path.join(parent_dir, '.env')
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key] = val.strip('"').strip("'")

from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import download_token
from email_automation.app_config import CLIENT_ID, CLIENT_SECRET, AUTHORITY, SCOPES, TOKEN_CACHE, FIREBASE_API_KEY
import requests


def list_user_ids():
    """List available user IDs from Firebase Storage."""
    url = f"https://firebasestorage.googleapis.com/v0/b/email-automation-cache.firebasestorage.app/o?prefix=msal_caches%2F&key={FIREBASE_API_KEY}"
    r = requests.get(url)
    data = r.json()
    user_ids = set()
    for item in data.get("items", []):
        parts = item["name"].split("/")
        if len(parts) == 3 and parts[0] == "msal_caches" and parts[2] == "msal_token_cache.bin":
            user_ids.add(parts[1])
    return list(user_ids)

# Default user ID for baylor.freelance@outlook.com
DEFAULT_USER_ID = "xG7jAeu8ceYVBhXQwDFRfvLmvpH2"


def get_access_token(user_id: str = None) -> str:
    """
    Get access token for the specified user.
    This is the ONLY correct way to get an Outlook access token.
    """
    user_id = user_id or DEFAULT_USER_ID

    # Download token from Firebase Storage
    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)

    # Load into MSAL cache
    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as f:
        cache.deserialize(f.read())

    # Create app and acquire token
    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if not accounts:
        raise RuntimeError(f"No account found for user {user_id}")

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise RuntimeError(f"Failed to acquire token for user {user_id}")

    return result["access_token"]


def list_inbox(user_id: str = None, limit: int = 20):
    """List recent inbox messages."""
    token = get_access_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
        headers=headers,
        params={
            "$top": str(limit),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,hasAttachments,bodyPreview"
        }
    )

    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text[:500]}")
        return []

    messages = resp.json().get("value", [])
    print(f"\n📬 Found {len(messages)} messages in inbox\n")

    for i, msg in enumerate(messages, 1):
        subj = msg.get("subject", "No subject")
        from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "?")
        received = msg.get("receivedDateTime", "")[:19]
        has_attach = "📎" if msg.get("hasAttachments") else "  "
        preview = msg.get("bodyPreview", "")[:60].replace("\n", " ")
        msg_id = msg.get("id", "")

        print(f"{i:2}. {has_attach} {received}")
        print(f"    From: {from_addr}")
        print(f"    Subject: {subj}")
        print(f"    Preview: {preview}...")
        print(f"    ID: {msg_id[:50]}...")
        print()

    return messages


def get_attachments(msg_id: str, user_id: str = None, save_dir: str = "/tmp"):
    """Download attachments from a message."""
    token = get_access_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Get attachments list
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}/attachments",
        headers=headers
    )

    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text[:500]}")
        return []

    attachments = resp.json().get("value", [])
    print(f"\n📎 Found {len(attachments)} attachments\n")

    saved_files = []
    for att in attachments:
        name = att.get("name", "unknown")
        content_type = att.get("contentType", "?")
        size = att.get("size", 0)
        content_bytes = att.get("contentBytes", "")

        print(f"  - {name} ({content_type}, {size} bytes)")

        if content_bytes:
            # Decode and save
            data = base64.b64decode(content_bytes)
            filepath = os.path.join(save_dir, name)
            with open(filepath, "wb") as f:
                f.write(data)
            print(f"    Saved to: {filepath}")
            saved_files.append(filepath)

    return saved_files


def show_users():
    """List all available user IDs."""
    users = list_user_ids()
    print(f"\n👥 Available users ({len(users)}):\n")
    for uid in users:
        marker = " (default)" if uid == DEFAULT_USER_ID else ""
        print(f"  {uid}{marker}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "inbox":
        user_id = sys.argv[2] if len(sys.argv) > 2 else None
        list_inbox(user_id)

    elif cmd == "attachments":
        if len(sys.argv) < 3:
            print("Usage: outlook_helper.py attachments <msg_id> [user_id]")
            sys.exit(1)
        msg_id = sys.argv[2]
        user_id = sys.argv[3] if len(sys.argv) > 3 else None
        get_attachments(msg_id, user_id)

    elif cmd == "users":
        show_users()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
