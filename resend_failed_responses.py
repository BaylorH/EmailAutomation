#!/usr/bin/env python3
"""
One-time script to resend failed response emails.
These responses were generated but failed to send due to Microsoft account block.

Usage:
    python resend_failed_responses.py [--dry-run]
"""

import os
import sys
import json
import atexit

# Add email_automation to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from msal import ConfidentialClientApplication, SerializableTokenCache
from firebase_helpers import download_token, upload_token
from google.cloud import firestore
from email_automation.clients import _fs, decode_token_payload
from email_automation.processing import send_reply_in_thread
from email_automation.app_config import CLIENT_ID, CLIENT_SECRET, AUTHORITY, SCOPES, TOKEN_CACHE, FIREBASE_API_KEY


def get_headers_for_user(user_id: str):
    """Get auth headers for a specific user (same as main.py)."""
    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=user_id)

    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as f:
        cache.deserialize(f.read())

    def _save_cache():
        if cache.has_state_changed:
            with open(TOKEN_CACHE, "w") as f:
                f.write(cache.serialize())
            upload_token(FIREBASE_API_KEY, input_file=TOKEN_CACHE, user_id=user_id)
            print(f"✅ Token cache uploaded for {user_id}")

    atexit.register(_save_cache)

    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if not accounts:
        print(f"⚠️ No account found for {user_id}")
        return None

    result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result or "access_token" not in result:
        print(f"❌ Silent auth failed for {user_id}")
        return None

    access_token = result["access_token"]
    print(f"🎯 Got access token; expires_in≈{result.get('expires_in')}s")

    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }


def get_pending_responses(user_id: str, date_filter: str = "2026-03-01"):
    """Find sheetChangeLog entries with unsent responses from a specific date."""
    change_logs = _fs.collection('users').document(user_id).collection('sheetChangeLog').stream()

    pending = []
    for log in change_logs:
        # Only entries from the specified date
        if date_filter not in log.id:
            continue
        # Skip "applied" entries (these are just confirmations)
        if 'applied' in log.id:
            continue

        data = log.to_dict()
        thread_id = data.get('threadId')
        email = data.get('email')

        # Get proposal JSON
        proposal = data.get('proposalJson', {})
        if isinstance(proposal, str):
            proposal = json.loads(proposal)

        response_email = proposal.get('response_email')

        # Skip if no response (e.g., needs_user_input correctly skipped)
        if not response_email:
            continue

        pending.append({
            'log_id': log.id,
            'thread_id': thread_id,
            'email': email,
            'response': response_email,
        })

    return pending


def get_latest_inbound_message_id(user_id: str, thread_id: str):
    """Get the most recent inbound message ID in a thread (to reply to)."""
    messages = _fs.collection('users').document(user_id).collection('threads').document(thread_id).collection('messages').stream()

    inbound_msgs = []
    for msg in messages:
        data = msg.to_dict()
        if data.get('direction') == 'inbound':
            inbound_msgs.append((msg.id, data.get('timestamp')))

    if inbound_msgs:
        # Sort by timestamp descending and return latest
        inbound_msgs.sort(key=lambda x: x[1] if x[1] else '', reverse=True)
        return inbound_msgs[0][0]

    # Fallback: use thread root ID
    return thread_id


def resend_responses(user_id: str, dry_run: bool = False, date_filter: str = "2026-03-01"):
    """Resend all pending responses for a user."""

    # Get auth headers
    print(f"Getting auth headers for user {user_id}...")
    headers = get_headers_for_user(user_id)
    if not headers:
        print("Failed to get auth headers. Is the token valid?")
        return False

    # Find pending responses
    pending = get_pending_responses(user_id, date_filter)
    print(f"\nFound {len(pending)} pending responses from {date_filter}")

    if not pending:
        print("No responses to resend!")
        return True

    success_count = 0
    for i, item in enumerate(pending):
        thread_id = item['thread_id']
        email = item['email']
        response = item['response']

        print(f"\n[{i+1}/{len(pending)}] Thread: {thread_id[:50]}...")
        print(f"    To: {email}")
        print(f"    Response: {response[:80]}...")

        if dry_run:
            print("    [DRY RUN] Would send this response")
            continue

        # Get latest inbound message ID to reply to
        msg_id = get_latest_inbound_message_id(user_id, thread_id)
        print(f"    Replying to: {msg_id[:50]}...")

        # Send the reply
        try:
            sent = send_reply_in_thread(
                user_id=user_id,
                headers=headers,
                body=response,
                current_msg_id=msg_id,
                recipient=email,
                thread_id=thread_id
            )

            if sent:
                print(f"    ✅ Successfully resent!")
                success_count += 1
            else:
                print(f"    ❌ Failed to resend")
        except Exception as e:
            print(f"    ❌ Error: {e}")

    print(f"\n{'='*50}")
    print(f"SUMMARY: Resent {success_count}/{len(pending)} responses")
    print(f"{'='*50}")
    return success_count == len(pending)


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print("=" * 50)
        print("DRY RUN MODE - No emails will be sent")
        print("=" * 50)

    # User ID with failed responses (your account)
    USER_ID = "NO7lVYVp6BaplKYEfMlWCgBnpdh2"

    # Date of the failed responses
    DATE_FILTER = "2026-03-01"

    print(f"Resending failed responses for user {USER_ID}")
    print(f"Date filter: {DATE_FILTER}\n")

    resend_responses(USER_ID, dry_run=dry_run, date_filter=DATE_FILTER)
