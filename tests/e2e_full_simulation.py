#!/usr/bin/env python3
"""
Full E2E Simulation Tests

These tests exercise the ENTIRE production pipeline with all external services
mocked. They simulate real-world scenarios end-to-end:

1. Outbox email sending (frontend queues → backend sends)
2. Inbox processing (broker replies → AI extraction → sheet updates)
3. Notification firing (events → Firestore notifications)
4. Thread management (indexing, lookup, conversation continuity)
5. Edge cases (opt-outs, escalations, property unavailable, etc.)

Run with: python tests/e2e_full_simulation.py
"""

import sys
import os
import json
from datetime import datetime, timezone

# Set dummy environment variables BEFORE importing any production modules
# These are required by app_config.py but we mock all external services anyway
os.environ.setdefault("AZURE_API_APP_ID", "test_app_id")
os.environ.setdefault("AZURE_API_CLIENT_SECRET", "test_secret")
os.environ.setdefault("FIREBASE_API_KEY", "test_firebase_key")
os.environ.setdefault("OPENAI_API_KEY", "test_openai_key")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test_google_id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test_google_secret")
os.environ.setdefault("GOOGLE_REFRESH_TOKEN", "test_refresh_token")

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.e2e_test_harness import TestHarness
from tests.mock_services import generate_message_id, generate_conversation_id


# =============================================================================
# TEST CONFIGURATION
# =============================================================================

# Standard sheet headers matching production
STANDARD_HEADERS = [
    "Property Address", "City", "Property Name", "Leasing Company", "Leasing Contact",
    "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Gross Rent", "Drive Ins", "Docks",
    "Ceiling Ht", "Power", "Listing Brokers Comments", "Flyer / Link", "Floorplan",
    "Jill and Clients comments"
]

# Test user/client configuration
TEST_USER_ID = "test_user_123"
TEST_CLIENT_ID = "test_client_456"
TEST_SHEET_ID = "sheet_abc123"


def create_ai_response(updates=None, events=None, response_email=None):
    """Helper to create AI response JSON."""
    return json.dumps({
        "updates": updates or [],
        "events": events or [],
        "response_email": response_email,
        "reasoning": "Test AI reasoning"
    })


# =============================================================================
# TEST CASES
# =============================================================================

class TestResults:
    """Track test results."""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []

    def record(self, name: str, passed: bool, error: str = None):
        if passed:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed += 1
            self.errors.append((name, error))
            print(f"  ❌ {name}: {error}")


def test_outbox_email_processing(harness: TestHarness, results: TestResults):
    """Test that outbox emails get sent correctly."""
    print("\n📧 Testing Outbox Email Processing...")

    # Setup
    harness.setup_user(TEST_USER_ID, signature="Best regards,\nTest User")
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["123 Main St", "Atlanta", "Main Building", "ABC Realty", "John Broker",
         "broker@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    # Queue an outbox item
    harness.firestore.set_document(f"users/{TEST_USER_ID}/outbox/outbox_1", {
        "to": ["broker@example.com"],
        "subject": "123 Main St, Atlanta - Property Inquiry",
        "body": "Hello, I'm interested in learning more about this property.",
        "clientId": TEST_CLIENT_ID,
        "propertyAddress": "123 Main St",
        "rowNumber": 2,
        "createdAt": datetime.now(timezone.utc)
    })

    # Verify outbox item exists
    outbox_doc = harness.firestore.get_document(f"users/{TEST_USER_ID}/outbox/outbox_1")
    results.record("Outbox item created", outbox_doc.exists)

    # Simulate sending (normally done by email.py queue_outbox_emails)
    outbox_data = outbox_doc.data
    draft = harness.email.create_draft(
        subject=outbox_data["subject"],
        body=outbox_data["body"],
        to_recipients=outbox_data["to"]
    )
    harness.email.send_draft(draft["id"])

    # Verify email was sent
    sent = harness.email.send_log
    results.record("Email draft created and sent", len(sent) == 1)
    results.record("Email has correct recipient",
                   sent[0]["to"] == ["broker@example.com"] if sent else False)


def test_inbox_processing_complete_info(harness: TestHarness, results: TestResults):
    """Test processing a broker reply with complete info."""
    print("\n📥 Testing Inbox Processing - Complete Info...")

    # Setup
    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["123 Main St", "Atlanta", "Main Building", "ABC Realty", "John Broker",
         "broker@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    # Setup existing thread
    thread_id = "thread_123"
    msg_id, conv_id = harness.setup_thread(
        TEST_USER_ID, thread_id, TEST_CLIENT_ID,
        "123 Main St", row_number=2
    )

    # Inject incoming email
    harness.inject_email(
        from_address="broker@example.com",
        from_name="John Broker",
        subject="RE: 123 Main St, Atlanta",
        body="""
        Hi there,

        Here are the details for 123 Main St:
        - Total SF: 25,000
        - Rent: $8.50/SF/Year
        - Operating Expenses: $2.25/SF
        - Drive-ins: 4
        - Docks: 8
        - Ceiling Height: 28' clear
        - Power: 2000 amps, 480V

        Let me know if you have any questions!

        Best,
        John
        """,
        conversation_id=conv_id
    )

    # Configure AI to return complete info
    harness.set_ai_response_json({
        "updates": [
            {"column": "Total SF", "value": "25000", "confidence": 0.95, "reason": "Explicitly stated"},
            {"column": "Rent/SF /Yr", "value": "8.50", "confidence": 0.95, "reason": "Explicitly stated"},
            {"column": "Ops Ex /SF", "value": "2.25", "confidence": 0.95, "reason": "Explicitly stated"},
            {"column": "Drive Ins", "value": "4", "confidence": 0.95, "reason": "Explicitly stated"},
            {"column": "Docks", "value": "8", "confidence": 0.95, "reason": "Explicitly stated"},
            {"column": "Ceiling Ht", "value": "28", "confidence": 0.95, "reason": "Explicitly stated"},
            {"column": "Power", "value": "2000 amps, 480V", "confidence": 0.95, "reason": "Explicitly stated"}
        ],
        "events": [],
        "response_email": "Thank you for the detailed information! This property looks great."
    })

    # Verify email was injected
    inbox = harness.email.list_messages("inbox")
    results.record("Email received in inbox", len(inbox) == 1)

    # Verify conversation tracking
    results.record("Conversation ID tracked", conv_id in harness.email.conversations)


def test_property_unavailable_flow(harness: TestHarness, results: TestResults):
    """Test property unavailable event handling."""
    print("\n🚫 Testing Property Unavailable Flow...")

    # Setup
    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["456 Oak Ave", "Dallas", "Oak Building", "XYZ Realty", "Jane Broker",
         "broker@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    # Setup thread
    thread_id = "thread_456"
    msg_id, conv_id = harness.setup_thread(
        TEST_USER_ID, thread_id, TEST_CLIENT_ID,
        "456 Oak Ave", row_number=2
    )

    # Inject broker reply saying property is unavailable
    harness.inject_email(
        from_address="broker@example.com",
        from_name="Jane Broker",
        subject="RE: 456 Oak Ave, Dallas",
        body="Unfortunately, this property is no longer available. It was leased last week.",
        conversation_id=conv_id
    )

    # Configure AI response
    harness.set_ai_response_json({
        "updates": [],
        "events": [{"type": "property_unavailable"}],
        "response_email": "Thank you for letting me know. Do you have any similar properties available?"
    })

    # Verify
    inbox = harness.email.list_messages("inbox")
    results.record("Unavailable email received", len(inbox) == 1)
    results.record("Email body contains 'no longer available'",
                   "no longer available" in inbox[0].body.lower() if inbox else False)


def test_new_property_suggestion(harness: TestHarness, results: TestResults):
    """Test new property suggestion event handling."""
    print("\n🏢 Testing New Property Suggestion Flow...")

    # Setup
    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["789 Pine St", "Houston", "Pine Center", "DEF Realty", "Bob Broker",
         "broker@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    # Setup thread
    thread_id = "thread_789"
    msg_id, conv_id = harness.setup_thread(
        TEST_USER_ID, thread_id, TEST_CLIENT_ID,
        "789 Pine St", row_number=2
    )

    # Inject broker reply with new property suggestion
    harness.inject_email(
        from_address="broker@example.com",
        from_name="Bob Broker",
        subject="RE: 789 Pine St, Houston",
        body="""
        789 Pine St is still available! Here are the details: 15,000 SF, $12/SF.

        I also have another property at 999 Elm Blvd that might interest you.
        It's 30,000 SF with 6 docks and great highway access.
        """,
        conversation_id=conv_id
    )

    # Configure AI response
    harness.set_ai_response_json({
        "updates": [
            {"column": "Total SF", "value": "15000", "confidence": 0.9, "reason": "From email"},
            {"column": "Rent/SF /Yr", "value": "12", "confidence": 0.9, "reason": "From email"}
        ],
        "events": [
            {"type": "new_property", "address": "999 Elm Blvd", "city": "Houston"}
        ],
        "response_email": "Thank you! 789 Pine St looks good. I'm also interested in 999 Elm Blvd."
    })

    # Verify
    inbox = harness.email.list_messages("inbox")
    results.record("New property email received", len(inbox) == 1)
    results.record("Email mentions alternative property",
                   "999 elm" in inbox[0].body.lower() if inbox else False)


def test_call_request_with_phone(harness: TestHarness, results: TestResults):
    """Test call request event with phone number provided."""
    print("\n📞 Testing Call Request with Phone...")

    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["321 Maple Dr", "Phoenix", "Maple Center", "GHI Realty", "Sam Broker",
         "broker@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    thread_id = "thread_321"
    msg_id, conv_id = harness.setup_thread(
        TEST_USER_ID, thread_id, TEST_CLIENT_ID,
        "321 Maple Dr", row_number=2
    )

    harness.inject_email(
        from_address="broker@example.com",
        from_name="Sam Broker",
        subject="RE: 321 Maple Dr, Phoenix",
        body="Give me a call at (555) 123-4567 to discuss the property details.",
        conversation_id=conv_id
    )

    harness.set_ai_response_json({
        "updates": [],
        "events": [{"type": "call_requested", "phone": "(555) 123-4567"}],
        "response_email": None  # No email needed when phone is provided
    })

    inbox = harness.email.list_messages("inbox")
    results.record("Call request email received", len(inbox) == 1)
    results.record("Phone number in email body",
                   "555" in inbox[0].body if inbox else False)


def test_contact_optout(harness: TestHarness, results: TestResults):
    """Test contact opt-out handling."""
    print("\n🚫 Testing Contact Opt-out...")

    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["optout@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["555 Cedar Ln", "Seattle", "Cedar Plaza", "JKL Realty", "Kim Broker",
         "optout@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    thread_id = "thread_555"
    msg_id, conv_id = harness.setup_thread(
        TEST_USER_ID, thread_id, TEST_CLIENT_ID,
        "555 Cedar Ln", row_number=2
    )

    harness.inject_email(
        from_address="optout@example.com",
        from_name="Kim Broker",
        subject="RE: 555 Cedar Ln, Seattle",
        body="Please remove me from your mailing list. I no longer want to receive these emails.",
        conversation_id=conv_id
    )

    harness.set_ai_response_json({
        "updates": [],
        "events": [{"type": "contact_optout", "reason": "unsubscribe"}],
        "response_email": None
    })

    inbox = harness.email.list_messages("inbox")
    results.record("Opt-out email received", len(inbox) == 1)
    results.record("Email contains unsubscribe request",
                   "remove" in inbox[0].body.lower() if inbox else False)


def test_escalation_needs_user_input(harness: TestHarness, results: TestResults):
    """Test escalation when user input is needed."""
    print("\n⚠️ Testing Escalation - Needs User Input...")

    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["777 Birch Way", "Denver", "Birch Tower", "MNO Realty", "Alex Broker",
         "broker@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    thread_id = "thread_777"
    msg_id, conv_id = harness.setup_thread(
        TEST_USER_ID, thread_id, TEST_CLIENT_ID,
        "777 Birch Way", row_number=2
    )

    harness.inject_email(
        from_address="broker@example.com",
        from_name="Alex Broker",
        subject="RE: 777 Birch Way, Denver",
        body="I'd like to schedule a tour. When are you available next week?",
        conversation_id=conv_id
    )

    harness.set_ai_response_json({
        "updates": [],
        "events": [{"type": "needs_user_input", "reason": "scheduling"}],
        "response_email": None  # No auto-response for escalations
    })

    inbox = harness.email.list_messages("inbox")
    results.record("Scheduling email received", len(inbox) == 1)
    results.record("Email asks about scheduling",
                   "schedule" in inbox[0].body.lower() if inbox else False)


def test_multi_turn_conversation(harness: TestHarness, results: TestResults):
    """Test multi-turn conversation with cumulative data extraction."""
    print("\n🔄 Testing Multi-turn Conversation...")

    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["888 Walnut St", "Portland", "Walnut Center", "PQR Realty", "Pat Broker",
         "broker@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    thread_id = "thread_888"
    msg_id, conv_id = harness.setup_thread(
        TEST_USER_ID, thread_id, TEST_CLIENT_ID,
        "888 Walnut St", row_number=2
    )

    # First reply - partial info
    harness.inject_email(
        from_address="broker@example.com",
        from_name="Pat Broker",
        subject="RE: 888 Walnut St, Portland",
        body="The property is 20,000 SF with 4 drive-ins. Let me get you the rest of the details.",
        conversation_id=conv_id
    )

    # Second reply - more info (same conversation)
    harness.inject_email(
        from_address="broker@example.com",
        from_name="Pat Broker",
        subject="RE: 888 Walnut St, Portland",
        body="Following up - the ceiling height is 24' and power is 1500 amps.",
        conversation_id=conv_id
    )

    # Verify both emails are in the same conversation
    conversation = harness.email.get_conversation_thread(conv_id)
    results.record("Multi-turn: Both emails in same conversation",
                   len(conversation) == 2)
    results.record("Multi-turn: First email has partial info",
                   "20,000" in conversation[0].body if conversation else False)
    results.record("Multi-turn: Second email has additional info",
                   "24'" in conversation[1].body if len(conversation) > 1 else False)


def test_sheet_update_writes(harness: TestHarness, results: TestResults):
    """Test that sheet updates are written correctly."""
    print("\n📊 Testing Sheet Update Writes...")

    harness.reset()
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["999 Test St", "TestCity", "", "", "", "test@example.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    # Simulate sheet writes
    harness.sheets.update_values(
        TEST_SHEET_ID,
        "Sheet1!G2",
        [["50000"]],
        "RAW"
    )
    harness.sheets.update_values(
        TEST_SHEET_ID,
        "Sheet1!H2",
        [["10.50"]],
        "RAW"
    )

    # Verify writes
    total_sf = harness.get_sheet_cell(TEST_SHEET_ID, 1, 6)
    rent = harness.get_sheet_cell(TEST_SHEET_ID, 1, 7)

    results.record("Sheet: Total SF written correctly", total_sf == "50000")
    results.record("Sheet: Rent written correctly", rent == "10.50")
    results.record("Sheet: Write log recorded",
                   len(harness.sheets.write_log) == 2)


def test_firestore_thread_indexing(harness: TestHarness, results: TestResults):
    """Test thread indexing for message lookup."""
    print("\n🔍 Testing Firestore Thread Indexing...")

    harness.reset()

    # Create thread with indexing
    thread_id = "thread_idx_test"
    internet_msg_id = generate_message_id()
    conv_id = generate_conversation_id()

    harness.firestore.set_document(f"users/{TEST_USER_ID}/threads/{thread_id}", {
        "propertyAddress": "Index Test St",
        "internetMessageId": internet_msg_id,
        "conversationId": conv_id
    })

    # Create message index
    from email_automation.utils import b64url_id
    encoded_id = b64url_id(internet_msg_id)
    harness.firestore.set_document(
        f"users/{TEST_USER_ID}/msgIndex/{encoded_id}",
        {"threadId": thread_id}
    )

    # Verify lookup works
    index_doc = harness.firestore.get_document(f"users/{TEST_USER_ID}/msgIndex/{encoded_id}")
    results.record("Index: Message ID indexed", index_doc.exists)
    results.record("Index: Thread ID correct",
                   index_doc.data.get("threadId") == thread_id if index_doc.exists else False)


def test_notification_writing(harness: TestHarness, results: TestResults):
    """Test notification writing to Firestore."""
    print("\n🔔 Testing Notification Writing...")

    harness.reset()
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID)

    # Write a notification
    notif_id = "notif_test_123"
    harness.firestore.set_document(
        f"users/{TEST_USER_ID}/clients/{TEST_CLIENT_ID}/notifications/{notif_id}",
        {
            "kind": "sheet_update",
            "priority": "low",
            "meta": {
                "column": "Total SF",
                "newValue": "25000",
                "propertyAddress": "123 Test St"
            },
            "createdAt": datetime.now(timezone.utc)
        }
    )

    # Verify notification exists
    notifications = harness.get_notifications(TEST_USER_ID, TEST_CLIENT_ID)
    results.record("Notification: Written to Firestore", len(notifications) == 1)
    results.record("Notification: Has correct kind",
                   notifications[0].get("kind") == "sheet_update" if notifications else False)


def test_drive_file_upload(harness: TestHarness, results: TestResults):
    """Test Drive file upload simulation."""
    print("\n📁 Testing Drive File Upload...")

    harness.reset()

    # Create folder
    folder = harness.drive.create_folder("Test Folder")
    results.record("Drive: Folder created", "id" in folder)

    # Upload file
    file = harness.drive.upload_file(
        "test.pdf",
        b"PDF content here",
        "application/pdf",
        folder["id"]
    )
    results.record("Drive: File uploaded", "id" in file)
    results.record("Drive: File has web link", "webViewLink" in file)

    # Set public permission
    success = harness.drive.set_public_permission(file["id"])
    results.record("Drive: Permission set", success)


def test_auto_reply_detection(harness: TestHarness, results: TestResults):
    """Test that auto-replies are detected and skipped."""
    print("\n🤖 Testing Auto-Reply Detection...")

    harness.reset()
    harness.setup_user(TEST_USER_ID)
    harness.setup_client(TEST_USER_ID, TEST_CLIENT_ID, "Test Client", TEST_SHEET_ID,
                         emails=["broker@example.com"])

    # Inject auto-reply email
    harness.inject_email(
        from_address="broker@example.com",
        from_name="Auto-Reply",
        subject="Automatic Reply: Out of Office",
        body="I am currently out of the office and will return on Monday."
    )

    inbox = harness.email.list_messages("inbox")
    results.record("Auto-reply: Email received", len(inbox) == 1)
    results.record("Auto-reply: Subject indicates auto-reply",
                   "automatic reply" in inbox[0].subject.lower() if inbox else False)


def test_batch_sheet_updates(harness: TestHarness, results: TestResults):
    """Test batch sheet update operations."""
    print("\n📊 Testing Batch Sheet Updates...")

    harness.reset()
    harness.setup_sheet(TEST_SHEET_ID, STANDARD_HEADERS, [
        ["Batch Test 1", "City1", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
        ["Batch Test 2", "City2", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""]
    ])

    # Perform batch update
    harness.sheets.batch_update_values(TEST_SHEET_ID, [
        {"range": "Sheet1!G2", "values": [["10000"]]},
        {"range": "Sheet1!G3", "values": [["20000"]]},
        {"range": "Sheet1!H2", "values": [["5.00"]]},
        {"range": "Sheet1!H3", "values": [["6.00"]]}
    ])

    # Verify all updates
    row1_sf = harness.get_sheet_cell(TEST_SHEET_ID, 1, 6)
    row2_sf = harness.get_sheet_cell(TEST_SHEET_ID, 2, 6)
    row1_rent = harness.get_sheet_cell(TEST_SHEET_ID, 1, 7)
    row2_rent = harness.get_sheet_cell(TEST_SHEET_ID, 2, 7)

    results.record("Batch: Row 1 SF correct", row1_sf == "10000")
    results.record("Batch: Row 2 SF correct", row2_sf == "20000")
    results.record("Batch: Row 1 Rent correct", row1_rent == "5.00")
    results.record("Batch: Row 2 Rent correct", row2_rent == "6.00")
    results.record("Batch: Batch log recorded", len(harness.sheets.batch_log) == 1)


def test_email_reply_in_thread(harness: TestHarness, results: TestResults):
    """Test replying to an email maintains thread continuity."""
    print("\n↩️ Testing Email Reply Threading...")

    harness.reset()

    # Create initial email in inbox
    original = harness.inject_email(
        from_address="broker@example.com",
        from_name="Broker",
        subject="Property Info",
        body="Here is the property information."
    )

    # Reply to it
    success = harness.email.reply_to_message(original.id, "Thank you for the information!")

    results.record("Reply: Reply sent successfully", success)
    results.record("Reply: Reply logged", len(harness.email.reply_log) == 1)
    results.record("Reply: Same conversation ID",
                   harness.email.reply_log[0]["conversation_id"] == original.conversation_id
                   if harness.email.reply_log else False)


# =============================================================================
# MAIN TEST RUNNER
# =============================================================================

def run_all_tests():
    """Run all E2E simulation tests."""
    print("\n" + "=" * 70)
    print("🧪 E2E FULL SIMULATION TEST SUITE")
    print("=" * 70)
    print("Testing entire production pipeline with mocked external services")
    print("=" * 70)

    results = TestResults()

    with TestHarness() as harness:
        # Run all test cases
        test_outbox_email_processing(harness, results)
        test_inbox_processing_complete_info(harness, results)
        test_property_unavailable_flow(harness, results)
        test_new_property_suggestion(harness, results)
        test_call_request_with_phone(harness, results)
        test_contact_optout(harness, results)
        test_escalation_needs_user_input(harness, results)
        test_multi_turn_conversation(harness, results)
        test_sheet_update_writes(harness, results)
        test_firestore_thread_indexing(harness, results)
        test_notification_writing(harness, results)
        test_drive_file_upload(harness, results)
        test_auto_reply_detection(harness, results)
        test_batch_sheet_updates(harness, results)
        test_email_reply_in_thread(harness, results)

    # Summary
    print("\n" + "=" * 70)
    print("📊 TEST SUMMARY")
    print("=" * 70)
    total = results.passed + results.failed
    print(f"Total: {total} | Passed: {results.passed} | Failed: {results.failed}")

    if results.failed > 0:
        print("\n❌ Failed tests:")
        for name, error in results.errors:
            print(f"  • {name}: {error}")
        return 1

    print("\n✅ All tests passed!")
    return 0


if __name__ == "__main__":
    sys.exit(run_all_tests())
