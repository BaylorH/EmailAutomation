#!/usr/bin/env python3
"""
E2E Test Helper Scripts
Quick verification commands for Firestore, Sheets, and Outlook
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import firestore
from datetime import datetime
import json

# Set credentials
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'service-account.json'

db = firestore.Client()
USER_ID = 'NO7lVYVp6BaplKYEfMlWCgBnpdh2'


def get_client():
    """Get the active test client"""
    clients = list(db.collection('users').document(USER_ID).collection('clients').stream())
    if not clients:
        print("No clients found")
        return None

    for c in clients:
        data = c.to_dict()
        print(f"\n=== Client: {data.get('name')} ===")
        print(f"ID: {c.id}")
        print(f"Status: {data.get('status')}")
        fu = data.get('followUpConfig')
        if fu:
            print(f"FollowUp Enabled: {fu.get('enabled')}")
            for i, f in enumerate(fu.get('followUps', [])):
                print(f"  Follow-up {i+1}: {f.get('waitTime')} {f.get('waitUnit')}")
        return c.id
    return None


def check_threads(client_id=None):
    """Check all threads for a client"""
    if not client_id:
        client_id = get_client()
    if not client_id:
        return

    print(f"\n=== Threads for {client_id} ===")
    threads = list(db.collection('users').document(USER_ID).collection('threads')
                   .where('clientId', '==', client_id).stream())

    print(f"Total: {len(threads)}")
    for t in threads:
        data = t.to_dict()
        subject = data.get('subject', 'N/A')[:40]
        status = data.get('status', 'N/A')
        fu_status = data.get('followUpStatus', 'N/A')
        msg_count = len(data.get('messages', []))

        # Color coding
        status_icon = {
            'active': '🟡',
            'paused': '🟠',
            'stopped': '⚫',
            'completed': '🟢'
        }.get(status, '⚪')

        print(f"\n{status_icon} {subject}")
        print(f"   Status: {status} | FollowUp: {fu_status} | Messages: {msg_count}")

        # Show follow-up timing if waiting
        fu_config = data.get('followUpConfig', {})
        next_at = fu_config.get('nextFollowUpAt')
        if next_at:
            print(f"   Next follow-up: {next_at}")


def check_outbox():
    """Check outbox items"""
    outbox = list(db.collection('users').document(USER_ID).collection('outbox').stream())
    print(f"\n=== Outbox: {len(outbox)} items ===")
    for o in outbox:
        data = o.to_dict()
        subj = data.get('subject', 'N/A')[:40]
        to = data.get('assignedEmails', ['N/A'])[0] if data.get('assignedEmails') else 'N/A'
        print(f"  - {subj} -> {to}")


def check_notifications(client_id=None):
    """Check notifications for a client"""
    if not client_id:
        client_id = get_client()
    if not client_id:
        return

    notifications = list(db.collection('users').document(USER_ID)
                         .collection('clients').document(client_id)
                         .collection('notifications').stream())

    print(f"\n=== Notifications: {len(notifications)} ===")

    # Sort by priority
    priority = {'action_needed': 0, 'row_completed': 1, 'property_unavailable': 2, 'sheet_update': 3}
    sorted_notifs = sorted(notifications, key=lambda n: priority.get(n.to_dict().get('kind', ''), 99))

    for n in sorted_notifs:
        data = n.to_dict()
        kind = data.get('kind', 'N/A')
        prop = data.get('rowAnchor', 'N/A')[:30]
        reason = data.get('meta', {}).get('reason', '')

        icon = {
            'action_needed': '🔴',
            'row_completed': '🟢',
            'property_unavailable': '⚫',
            'sheet_update': '🔵'
        }.get(kind, '⚪')

        print(f"  {icon} [{kind}] {prop}")
        if reason:
            print(f"      Reason: {reason}")


def clear_all():
    """Clear all test data"""
    print("Clearing all test data...")

    # Clear outbox
    for o in db.collection('users').document(USER_ID).collection('outbox').stream():
        o.reference.delete()

    # Clear threads
    for t in db.collection('users').document(USER_ID).collection('threads').stream():
        t.reference.delete()

    # Clear msgIndex
    for m in db.collection('users').document(USER_ID).collection('msgIndex').stream():
        m.reference.delete()

    # Clear convIndex
    for c in db.collection('users').document(USER_ID).collection('convIndex').stream():
        c.reference.delete()

    # Clear clients and notifications
    for c in db.collection('users').document(USER_ID).collection('clients').stream():
        for n in c.reference.collection('notifications').stream():
            n.reference.delete()
        c.reference.delete()

    print("Done!")


def status_report():
    """Full status report"""
    print("=" * 60)
    print(f"E2E TEST STATUS REPORT - {datetime.now().strftime('%H:%M:%S')}")
    print("=" * 60)

    client_id = get_client()
    if client_id:
        check_threads(client_id)
        check_notifications(client_id)
    check_outbox()


def trigger_workflow():
    """Trigger the GitHub Actions workflow"""
    import subprocess
    result = subprocess.run(
        ['gh', 'workflow', 'run', 'email.yml', '--repo', 'BaylorH/EmailAutomation'],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("✅ Workflow triggered successfully")
    else:
        print(f"❌ Failed to trigger workflow: {result.stderr}")


def workflow_status():
    """Check recent workflow runs"""
    import subprocess
    result = subprocess.run(
        ['gh', 'run', 'list', '--repo', 'BaylorH/EmailAutomation', '--limit', '3'],
        capture_output=True, text=True
    )
    print("\n=== Recent Workflow Runs ===")
    print(result.stdout)


def fetch_outlook_conversations():
    """
    Fetch full email conversations from Outlook using the same method as main.py.
    Downloads token from Firebase, uses MSAL to get access token, calls Graph API.
    """
    from msal import ConfidentialClientApplication, SerializableTokenCache
    from firebase_helpers import download_token
    from email_automation.app_config import CLIENT_ID, CLIENT_SECRET, AUTHORITY, SCOPES, TOKEN_CACHE, FIREBASE_API_KEY
    import requests
    import re

    # Map Firebase UID to MSAL cache user ID
    # The token cache is stored under the MSAL user ID, not Firebase UID
    MSAL_USER_ID = "5gUMpneceaOWOeY7HNOlYHNyaD53"  # Jill's MSAL user ID

    print("📥 Downloading token from Firebase...")
    download_token(FIREBASE_API_KEY, output_file=TOKEN_CACHE, user_id=MSAL_USER_ID)

    cache = SerializableTokenCache()
    with open(TOKEN_CACHE, "r") as f:
        cache.deserialize(f.read())

    app = ConfidentialClientApplication(
        CLIENT_ID,
        client_credential=CLIENT_SECRET,
        authority=AUTHORITY,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if not accounts:
        print("❌ No account found in token cache")
        return

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        print("❌ Failed to acquire token")
        return

    access_token = result["access_token"]
    print(f"✅ Got access token: {access_token[:40]}...")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Keywords for our test campaign
    keywords = ["Commerce", "Industrial", "Warehouse", "Distribution", "Logistics", "Storage", "Tech Park"]

    print("\n" + "=" * 70)
    print("OUTLOOK SENT ITEMS - JILL'S OUTBOUND EMAILS")
    print("=" * 70)

    # Fetch sent items with body
    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders/SentItems/messages",
        headers=headers,
        params={
            "$top": "50",
            "$orderby": "sentDateTime desc",
            "$select": "subject,sentDateTime,toRecipients,body,conversationId"
        }
    )

    if resp.status_code != 200:
        print(f"❌ Failed to fetch sent items: {resp.status_code}")
        print(resp.text[:500])
        return

    sent_messages = resp.json().get("value", [])

    # Group by conversation
    conversations = {}
    for msg in sent_messages:
        subject = msg.get("subject", "")
        if any(kw in subject for kw in keywords):
            conv_id = msg.get("conversationId", "unknown")
            if conv_id not in conversations:
                conversations[conv_id] = {"subject": subject, "messages": []}
            conversations[conv_id]["messages"].append({
                "direction": "SENT",
                "time": msg.get("sentDateTime", "")[:19],
                "to": [r["emailAddress"]["address"] for r in msg.get("toRecipients", [])],
                "body": _clean_html(msg.get("body", {}).get("content", ""))
            })

    # Also fetch inbox for broker replies
    print("\n" + "=" * 70)
    print("OUTLOOK INBOX - BROKER REPLIES")
    print("=" * 70)

    resp = requests.get(
        "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
        headers=headers,
        params={
            "$top": "50",
            "$orderby": "receivedDateTime desc",
            "$select": "subject,receivedDateTime,from,body,conversationId"
        }
    )

    if resp.status_code == 200:
        inbox_messages = resp.json().get("value", [])
        for msg in inbox_messages:
            subject = msg.get("subject", "")
            if any(kw in subject for kw in keywords):
                conv_id = msg.get("conversationId", "unknown")
                if conv_id not in conversations:
                    conversations[conv_id] = {"subject": subject, "messages": []}
                conversations[conv_id]["messages"].append({
                    "direction": "RECEIVED",
                    "time": msg.get("receivedDateTime", "")[:19],
                    "from": msg.get("from", {}).get("emailAddress", {}).get("address", ""),
                    "body": _clean_html(msg.get("body", {}).get("content", ""))
                })

    # Print conversations
    print("\n" + "=" * 70)
    print("FULL CONVERSATIONS BY PROPERTY")
    print("=" * 70)

    for conv_id, conv in conversations.items():
        # Sort messages by time
        conv["messages"].sort(key=lambda m: m.get("time", ""))

        # Extract property name from subject
        subject = conv["subject"].replace("RE: ", "").replace("Re: ", "")

        print(f"\n{'='*70}")
        print(f"📧 {subject}")
        print(f"   Conversation ID: {conv_id[:40]}...")
        print(f"   Messages: {len(conv['messages'])}")
        print("-" * 70)

        for i, msg in enumerate(conv["messages"], 1):
            direction = msg["direction"]
            time = msg["time"]
            body = msg["body"][:800]  # Truncate for readability

            if direction == "SENT":
                print(f"\n  [{i}] 📤 SENT at {time}")
                to = msg.get("to", [])
                if to:
                    print(f"      To: {', '.join(to)}")
            else:
                print(f"\n  [{i}] 📥 RECEIVED at {time}")
                print(f"      From: {msg.get('from', 'unknown')}")

            print(f"      ---")
            # Indent the body
            for line in body.split('\n')[:20]:  # Max 20 lines
                if line.strip():
                    print(f"      {line.strip()}")
        print()


def _clean_html(html_content):
    """Strip HTML tags and clean up content"""
    import re
    # Remove style tags
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', '\n', text)
    # Clean up entities
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    # Clean up whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='E2E Test Helpers')
    parser.add_argument('command', choices=['status', 'threads', 'outbox', 'notifications',
                                            'clear', 'trigger', 'workflow', 'client', 'outlook'],
                        help='Command to run')
    args = parser.parse_args()

    if args.command == 'status':
        status_report()
    elif args.command == 'threads':
        check_threads()
    elif args.command == 'outbox':
        check_outbox()
    elif args.command == 'notifications':
        check_notifications()
    elif args.command == 'clear':
        clear_all()
    elif args.command == 'trigger':
        trigger_workflow()
    elif args.command == 'workflow':
        workflow_status()
    elif args.command == 'client':
        get_client()
    elif args.command == 'outlook':
        fetch_outlook_conversations()
