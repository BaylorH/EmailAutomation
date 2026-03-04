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


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='E2E Test Helpers')
    parser.add_argument('command', choices=['status', 'threads', 'outbox', 'notifications',
                                            'clear', 'trigger', 'workflow', 'client'],
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
