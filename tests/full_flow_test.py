#!/usr/bin/env python3
"""
Full Flow E2E Test - Simulates complete user interaction sequences

This test runs as a script (no server needed) and simulates:
1. User uploads Excel → Client created with properties
2. User launches campaign → Outbox populated
3. Backend runs (like GitHub Actions) → Emails "sent", threads created
4. Broker replies arrive → Backend processes, extracts data
5. Notifications created → User sees them in UI
6. User responds to notifications → Approves new property, etc.
7. Sheets updated → Verify final state

Usage:
    # Set environment variables
    export FIRESTORE_EMULATOR_HOST=127.0.0.1:8080
    export OPENAI_API_KEY=sk-...

    # Run full flow test
    python tests/full_flow_test.py

    # Run specific scenario
    python tests/full_flow_test.py --scenario call_requested

    # Run with mock OpenAI (no API key needed)
    python tests/full_flow_test.py --mock-openai
"""

import os
import sys

# CRITICAL: Set E2E_TEST_MODE BEFORE any other imports
# This must happen before app_config.py is loaded
os.environ['E2E_TEST_MODE'] = 'true'

import json
import argparse
from datetime import datetime
from typing import Dict, List, Optional, Any
from unittest.mock import patch, MagicMock

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Check if we're in test mode
USE_EMULATOR = os.environ.get('FIRESTORE_EMULATOR_HOST') is not None
MOCK_OPENAI = '--mock-openai' in sys.argv

if USE_EMULATOR:
    print(f"🧪 Using Firestore emulator at {os.environ['FIRESTORE_EMULATOR_HOST']}")
else:
    print("⚠️  WARNING: Not using emulator - will affect production data!")
    print("   Set FIRESTORE_EMULATOR_HOST=127.0.0.1:8080 for safe testing")

if MOCK_OPENAI:
    print("🤖 Using mock OpenAI responses")


class MockGraphAPI:
    """Mock Microsoft Graph API for email operations."""

    def __init__(self):
        self.sent_emails: List[Dict] = []
        self.inbox_messages: List[Dict] = []
        self.draft_counter = 0

    def inject_reply(self, from_email: str, body: str, subject: str = "RE: Property"):
        """Inject a broker reply into the mock inbox."""
        self.draft_counter += 1
        msg_id = f"mock-reply-{self.draft_counter}"

        # Find the original sent email to get the conversationId for threading
        original = next((e for e in self.sent_emails if from_email in (e.get('to') or [])), None)
        conv_id = original.get('conversationId', f"conv-{msg_id}") if original else f"conv-{msg_id}"
        in_reply_to = f"<draft-{len(self.sent_emails)}@mock.test>" if self.sent_emails else None

        message = {
            "id": msg_id,
            "from": {"emailAddress": {"address": from_email, "name": "Broker"}},
            "subject": subject,
            "body": {"content": body, "contentType": "text"},
            "bodyPreview": body[:200],
            "receivedDateTime": datetime.utcnow().isoformat() + "Z",
            "sentDateTime": datetime.utcnow().isoformat() + "Z",
            "conversationId": conv_id,
            "internetMessageId": f"<{msg_id}@mock.test>",
            "isRead": False,
            "internetMessageHeaders": [],
        }

        if in_reply_to:
            message["internetMessageHeaders"].append({
                "name": "In-Reply-To",
                "value": in_reply_to
            })
            message["internetMessageHeaders"].append({
                "name": "References",
                "value": in_reply_to
            })

        self.inbox_messages.append(message)
        return msg_id

    def mock_request(self, method: str, url: str, **kwargs):
        """Mock requests to Graph API."""

        class MockResponse:
            def __init__(self, json_data, status_code):
                self._json_data = json_data
                self.status_code = status_code
                self.text = json.dumps(json_data) if json_data else ""
                self.ok = status_code < 400

            def json(self):
                return self._json_data

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise Exception(f"HTTP {self.status_code}")

        if "/me/mailFolders/Inbox/messages" in url and method == "get":
            return MockResponse({"value": self.inbox_messages}, 200)

        elif "/me/messages" in url and method == "post":
            self.draft_counter += 1
            draft_id = f"draft-{self.draft_counter}"
            data = kwargs.get('json', {})

            # Record the sent email
            self.sent_emails.append({
                "id": draft_id,
                "to": [r.get("emailAddress", {}).get("address") for r in data.get("toRecipients", [])],
                "subject": data.get("subject"),
                "body": data.get("body", {}).get("content"),
                "conversationId": f"conv-{draft_id}",
            })

            return MockResponse({
                "id": draft_id,
                "internetMessageId": f"<{draft_id}@mock.test>",
                "conversationId": f"conv-{draft_id}"
            }, 200)

        elif "/send" in url:
            return MockResponse(None, 202)

        elif method == "patch":
            return MockResponse(None, 200)

        elif "/me/messages/" in url and method == "get":
            # Get message details
            msg_id = url.split("/me/messages/")[1].split("?")[0]
            return MockResponse({
                "id": msg_id,
                "internetMessageId": f"<{msg_id}@mock.test>",
                "conversationId": f"conv-{msg_id}"
            }, 200)

        return MockResponse({"error": "Not found"}, 404)


class FullFlowTest:
    """
    Full flow E2E test that simulates complete user interaction sequences.

    This test doesn't require a server - it directly calls backend functions
    at the points where the frontend would wait for backend processing.
    """

    def __init__(self, user_id: str = "test-user-flow"):
        self.user_id = user_id
        self.mock_graph = MockGraphAPI()
        self.test_results = []
        self._patches = []

        # Import Firestore
        from google.cloud import firestore
        self.db = firestore.Client()

    def __enter__(self):
        self._start_mocks()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._stop_mocks()

    def _start_mocks(self):
        """Start mocking external APIs."""
        import requests

        def mock_request(method):
            original = getattr(requests, method)
            def wrapper(url, *args, **kwargs):
                if "graph.microsoft.com" in url:
                    return self.mock_graph.mock_request(method, url, **kwargs)
                return original(url, *args, **kwargs)
            return wrapper

        self._patches.append(patch('requests.get', mock_request('get')))
        self._patches.append(patch('requests.post', mock_request('post')))
        self._patches.append(patch('requests.patch', mock_request('patch')))

        for p in self._patches:
            p.start()

    def _stop_mocks(self):
        """Stop mocking."""
        for p in self._patches:
            p.stop()
        self._patches.clear()

    # =========================================================================
    # STEP 1: Simulate User Uploading Excel / Creating Client
    # =========================================================================

    def create_client(self, name: str, properties: List[Dict]) -> str:
        """
        Simulate: User uploads Excel file and creates a client.

        In the real app, this happens when:
        1. User clicks "Add Client"
        2. User uploads Excel file
        3. Frontend creates client doc with emailToProperties mapping
        """
        print(f"\n📤 STEP 1: User creates client '{name}' with {len(properties)} properties")

        # Build emailToProperties mapping (like frontend does)
        email_to_props = {}
        for prop in properties:
            email = prop.get('email', '')
            if email:
                if email not in email_to_props:
                    email_to_props[email] = []
                email_to_props[email].append({
                    'rowIndex': prop.get('rowIndex', 0),
                    'address': prop.get('address', ''),
                    'city': prop.get('city', ''),
                    'contact': prop.get('contact', ''),
                })

        # Create client document
        client_ref = self.db.collection('users').document(self.user_id) \
            .collection('clients').document()

        client_data = {
            'name': name,
            'assignedEmails': list(email_to_props.keys()),
            'emailToProperties': email_to_props,
            'sheetId': f'mock-sheet-{client_ref.id}',  # Would be real Google Sheet ID
            'createdAt': datetime.utcnow(),
            'status': 'active',
        }
        client_ref.set(client_data)

        print(f"   ✅ Created client {client_ref.id}")
        print(f"   📧 {len(email_to_props)} unique broker emails")

        return client_ref.id

    # =========================================================================
    # STEP 2: Simulate User Launching Campaign
    # =========================================================================

    def launch_campaign(self, client_id: str, script: str, properties: List[Dict]) -> List[str]:
        """
        Simulate: User clicks "Get Started" and launches email campaign.

        In the real app, this happens when:
        1. User opens StartProjectModal
        2. User customizes email script
        3. User clicks "Send Emails"
        4. Frontend creates outbox entries for each property/broker
        """
        print(f"\n📧 STEP 2: User launches campaign for {len(properties)} properties")

        outbox_ids = []
        for prop in properties:
            # Create outbox entry (like frontend does)
            outbox_ref = self.db.collection('users').document(self.user_id) \
                .collection('outbox').document()

            outbox_data = {
                'clientId': client_id,
                'email': prop.get('email', ''),
                'assignedEmails': [prop.get('email', '')],
                'script': script.replace('[NAME]', prop.get('contact', 'there').split()[0]),
                'subject': f"RE: {prop.get('address', 'Property')}",
                'property': {
                    'address': prop.get('address', ''),
                    'city': prop.get('city', ''),
                    'rowIndex': prop.get('rowIndex', 0),
                },
                'createdAt': datetime.utcnow(),
            }
            outbox_ref.set(outbox_data)
            outbox_ids.append(outbox_ref.id)

        print(f"   ✅ Created {len(outbox_ids)} outbox entries")
        return outbox_ids

    # =========================================================================
    # STEP 3: Trigger Backend Processing (Outbox → Send)
    # =========================================================================

    def run_backend_send(self) -> Dict:
        """
        Simulate: GitHub Actions runs every 30 min and processes outbox.

        This is where we call the REAL backend code instead of a server.
        """
        print(f"\n⚙️  STEP 3: Backend processes outbox (like GitHub Actions)")

        from email_automation.email import send_outboxes

        headers = {"Authorization": "Bearer mock-token"}

        with self:
            result = send_outboxes(self.user_id, headers)

        sent_count = len(self.mock_graph.sent_emails)
        print(f"   ✅ Sent {sent_count} emails")
        print(f"   📤 Emails: {[e['to'] for e in self.mock_graph.sent_emails]}")

        return {'sent_count': sent_count, 'emails': self.mock_graph.sent_emails}

    # =========================================================================
    # STEP 4: Simulate Broker Reply
    # =========================================================================

    def inject_broker_reply(self, from_email: str, body: str, subject: str = "RE: Property"):
        """
        Simulate: Broker receives email and replies.

        This is what we're testing - different broker responses trigger
        different backend behaviors.
        """
        print(f"\n📨 STEP 4: Broker replies from {from_email}")
        print(f"   Message: {body[:100]}...")

        return self.mock_graph.inject_reply(from_email, body, subject)

    # =========================================================================
    # STEP 5: Trigger Backend Processing (Inbox → Extract)
    # =========================================================================

    def run_backend_process(self) -> Dict:
        """
        Simulate: GitHub Actions runs and processes inbox replies.

        This is where AI extraction happens and notifications are created.
        """
        print(f"\n⚙️  STEP 5: Backend processes inbox (AI extraction)")

        from email_automation.processing import scan_inbox_against_index

        headers = {"Authorization": "Bearer mock-token"}

        with self:
            results = scan_inbox_against_index(self.user_id, headers)

        print(f"   ✅ Processed {len(results) if results else 0} messages")

        return {'processed': results or []}

    # =========================================================================
    # STEP 6: Check Notifications (What User Would See)
    # =========================================================================

    def get_notifications(self, client_id: str) -> List[Dict]:
        """
        Check: What notifications would the user see in the UI?

        This verifies that the backend created appropriate notifications.
        """
        print(f"\n🔔 STEP 6: Checking notifications for user")

        notifs_ref = self.db.collection('users').document(self.user_id) \
            .collection('clients').document(client_id) \
            .collection('notifications')

        notifs = []
        for doc in notifs_ref.stream():
            notifs.append({'id': doc.id, **doc.to_dict()})

        print(f"   📬 {len(notifs)} notifications:")
        for n in notifs:
            kind = n.get('kind', 'unknown')
            meta = n.get('meta', {})
            print(f"      - {kind}: {meta.get('reason', meta.get('column', 'N/A'))}")

        return notifs

    # =========================================================================
    # STEP 7: Check Sheet State (What Got Updated)
    # =========================================================================

    def get_sheet_updates(self) -> List[Dict]:
        """
        Check: What sheet updates were recorded?

        In production, this would be actual Google Sheets changes.
        For testing, we check the sheetUpdates collection.
        """
        print(f"\n📊 STEP 7: Checking sheet updates")

        updates_ref = self.db.collection('users').document(self.user_id) \
            .collection('sheetUpdates')

        updates = []
        for doc in updates_ref.stream():
            updates.append({'id': doc.id, **doc.to_dict()})

        print(f"   ✏️  {len(updates)} updates:")
        for u in updates:
            print(f"      - {u.get('column')}: {u.get('value')}")

        return updates

    # =========================================================================
    # STEP 8: Simulate User Responding to Notification
    # =========================================================================

    def user_approves_new_property(self, notification_id: str, client_id: str):
        """
        Simulate: User clicks "Approve" on new property suggestion.

        In the real app, this opens NewPropertyRequestModal and user:
        1. Reviews the suggested property
        2. Edits details if needed
        3. Clicks "Approve & Add"
        """
        print(f"\n👤 STEP 8: User approves new property")

        # Get the notification
        notif_ref = self.db.collection('users').document(self.user_id) \
            .collection('clients').document(client_id) \
            .collection('notifications').document(notification_id)

        notif = notif_ref.get().to_dict()
        if not notif:
            print("   ❌ Notification not found")
            return

        meta = notif.get('meta', {})
        print(f"   Property: {meta.get('address')}")
        print(f"   Contact: {meta.get('suggestedEmail')}")

        # Update notification status (like frontend does)
        notif_ref.update({
            'status': 'approved',
            'approvedAt': datetime.utcnow(),
        })

        print("   ✅ Property approved")

    # =========================================================================
    # CLEANUP
    # =========================================================================

    def cleanup(self):
        """Clean up test data."""
        print(f"\n🧹 Cleaning up test data for {self.user_id}")

        user_ref = self.db.collection('users').document(self.user_id)

        # Delete collections
        for coll_name in ['clients', 'outbox', 'threads', 'sheetUpdates', 'msgIndex', 'convIndex']:
            coll = user_ref.collection(coll_name)
            for doc in coll.stream():
                # Delete subcollections first
                for subcoll_name in ['notifications', 'messages']:
                    subcoll = doc.reference.collection(subcoll_name)
                    for subdoc in subcoll.stream():
                        subdoc.reference.delete()
                doc.reference.delete()

        print("   ✅ Cleaned up")


def run_scenario(scenario_name: str, test: FullFlowTest):
    """Run a specific test scenario."""

    # Load conversation fixture
    conv_path = f"tests/conversations/{scenario_name}.json"
    if not os.path.exists(conv_path):
        # Try edge cases
        conv_path = f"tests/conversations/edge_cases/{scenario_name}.json"

    if not os.path.exists(conv_path):
        print(f"❌ Scenario not found: {scenario_name}")
        return False

    with open(conv_path) as f:
        conv = json.load(f)

    print(f"\n{'='*70}")
    print(f"SCENARIO: {conv.get('description', scenario_name)}")
    print(f"{'='*70}")

    # Create test property from conversation
    property_data = {
        'address': conv.get('property', '123 Test St'),
        'city': conv.get('city', 'Test City'),
        'email': 'broker@test.com',
        'contact': 'Test Broker',
        'rowIndex': 3,
    }

    # Step 1: Create client
    client_id = test.create_client(f"Test - {scenario_name}", [property_data])

    # Step 2: Launch campaign
    outbound_msg = next((m for m in conv.get('messages', []) if m['direction'] == 'outbound'), None)
    script = outbound_msg['content'] if outbound_msg else "Test email"
    test.launch_campaign(client_id, script, [property_data])

    # Step 3: Backend sends emails
    test.run_backend_send()

    # Step 4: Simulate broker reply
    inbound_msg = next((m for m in conv.get('messages', []) if m['direction'] == 'inbound'), None)
    if inbound_msg:
        test.inject_broker_reply('broker@test.com', inbound_msg['content'])

    # Step 5: Backend processes reply
    test.run_backend_process()

    # Step 6: Check notifications
    notifs = test.get_notifications(client_id)

    # Step 7: Check sheet updates
    updates = test.get_sheet_updates()

    # Validate against expected
    expected_events = conv.get('expected_events', [])
    expected_updates = conv.get('expected_updates', [])
    expected_notifs = conv.get('expected_notifications', [])

    print(f"\n{'='*70}")
    print("VALIDATION")
    print(f"{'='*70}")

    # Check events
    actual_events = [n.get('meta', {}).get('reason', n.get('kind')) for n in notifs]
    print(f"   Expected events: {expected_events}")
    print(f"   Actual events:   {actual_events}")

    # Check updates
    actual_columns = [u.get('column') for u in updates]
    expected_columns = [u.get('column') for u in expected_updates]
    print(f"   Expected columns: {expected_columns}")
    print(f"   Actual columns:   {actual_columns}")

    passed = True
    for event in expected_events:
        if event not in str(actual_events):
            print(f"   ❌ Missing event: {event}")
            passed = False

    for col in expected_columns:
        if col not in actual_columns:
            print(f"   ❌ Missing update: {col}")
            passed = False

    if passed:
        print(f"\n   ✅ SCENARIO PASSED")
    else:
        print(f"\n   ❌ SCENARIO FAILED")

    return passed


def main():
    parser = argparse.ArgumentParser(description="Full Flow E2E Test")
    parser.add_argument('--scenario', help='Run specific scenario')
    parser.add_argument('--mock-openai', action='store_true', help='Mock OpenAI responses')
    parser.add_argument('--list', action='store_true', help='List available scenarios')
    parser.add_argument('--cleanup-only', action='store_true', help='Only cleanup test data')
    args = parser.parse_args()

    if args.list:
        print("Available scenarios:")
        import glob
        for f in glob.glob("tests/conversations/*.json"):
            name = os.path.basename(f).replace('.json', '')
            print(f"  - {name}")
        for f in glob.glob("tests/conversations/edge_cases/*.json"):
            name = os.path.basename(f).replace('.json', '')
            print(f"  - {name} (edge case)")
        return

    test = FullFlowTest()

    if args.cleanup_only:
        test.cleanup()
        return

    try:
        if args.scenario:
            # Run specific scenario
            passed = run_scenario(args.scenario, test)
            sys.exit(0 if passed else 1)
        else:
            # Run all main scenarios
            scenarios = [
                '699_industrial_park_dr',  # Call requested
                '1_kuhlke_dr',              # Complete info
                '1_randolph_ct',            # Partial info + question
                '135_trade_center_court',   # Scheduling request
                '2058_gordon_hwy',          # Different person replies
            ]

            results = []
            for scenario in scenarios:
                test.cleanup()  # Clean between scenarios
                passed = run_scenario(scenario, test)
                results.append((scenario, passed))

            print(f"\n{'='*70}")
            print("SUMMARY")
            print(f"{'='*70}")

            passed_count = sum(1 for _, p in results if p)
            for scenario, passed in results:
                status = "✅ PASS" if passed else "❌ FAIL"
                print(f"   {status} - {scenario}")

            print(f"\nTotal: {passed_count}/{len(results)} passed")
            sys.exit(0 if passed_count == len(results) else 1)

    finally:
        test.cleanup()


if __name__ == "__main__":
    main()
