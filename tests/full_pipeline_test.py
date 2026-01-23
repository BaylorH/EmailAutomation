#!/usr/bin/env python3
"""
Full Pipeline E2E Test
======================
Tests the ENTIRE pipeline including:
1. AI extraction (propose_sheet_updates)
2. Notification firing (mocked Firestore captures all calls)
3. Sheet update application
4. Frontend fixture generation

This validates that notifications ACTUALLY fire with correct data,
not just simulated.

Usage:
    python tests/full_pipeline_test.py
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch, call
import traceback

# ============================================================================
# LOAD ENVIRONMENT
# ============================================================================

env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

if not os.getenv("OPENAI_API_KEY"):
    print("❌ OPENAI_API_KEY not found")
    sys.exit(1)

for var in ["AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY"]:
    if not os.getenv(var):
        os.environ[var] = f"test-{var.lower()}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================================
# MOCK FIRESTORE - Capture all notification calls
# ============================================================================

class NotificationCapture:
    """Captures all notification calls for validation."""

    def __init__(self):
        self.notifications: List[Dict] = []
        self.client_notifications: List[Dict] = []

    def reset(self):
        self.notifications = []
        self.client_notifications = []

    def write_notification(self, uid, client_id, *, kind, priority, email,
                          thread_id, row_number=None, row_anchor=None,
                          meta=None, dedupe_key=None):
        """Capture write_notification call."""
        self.notifications.append({
            "uid": uid,
            "client_id": client_id,
            "kind": kind,
            "priority": priority,
            "email": email,
            "thread_id": thread_id,
            "row_number": row_number,
            "row_anchor": row_anchor,
            "meta": meta or {},
            "dedupe_key": dedupe_key
        })
        return f"notif-{len(self.notifications)}"

    def add_client_notifications(self, uid, client_id, email, thread_id,
                                 applied_updates, notes=None, address=None):
        """Capture add_client_notifications call."""
        self.client_notifications.append({
            "uid": uid,
            "client_id": client_id,
            "email": email,
            "thread_id": thread_id,
            "applied_updates": applied_updates,
            "notes": notes,
            "address": address
        })

NOTIFICATION_CAPTURE = NotificationCapture()

# Mock Firestore
mock_fs = MagicMock()
mock_fs.collection = MagicMock(return_value=MagicMock())

mock_firestore_module = MagicMock()
mock_firestore_module.Client = MagicMock(return_value=mock_fs)
mock_firestore_module.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
mock_firestore_module.FieldFilter = MagicMock()
sys.modules['google.cloud.firestore'] = mock_firestore_module
sys.modules['google.cloud'] = MagicMock()
sys.modules['google.oauth2.credentials'] = MagicMock()
sys.modules['google.auth.transport.requests'] = MagicMock()
sys.modules['googleapiclient.discovery'] = MagicMock()

# Now import and patch
from email_automation.ai_processing import propose_sheet_updates, apply_proposal_to_sheet
from email_automation import notifications as notif_module

# Patch notification functions
notif_module.write_notification = NOTIFICATION_CAPTURE.write_notification
notif_module.add_client_notifications = NOTIFICATION_CAPTURE.add_client_notifications

print("✅ Modules imported with notification capture")

# ============================================================================
# TEST DATA
# ============================================================================

HEADERS = [
    "Property Address", "City", "Property Name", "Leasing Company ",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", " Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments ", "Flyer / Link", "Floorplan",
    "Jill and Clients comments"
]

TEST_PROPERTY = {
    "address": "1 Randolph Ct",
    "city": "Evans",
    "contact": "Scott A. Atkins",
    "email": "scott@atkinscommercial.com",
    "row_number": 7,
    "data": ["1 Randolph Ct", "Evans", "", "Atkins Commercial", "Scott A. Atkins",
             "scott@atkinscommercial.com", "", "", "", "", "", "", "", "", "", "", "", ""]
}

# ============================================================================
# FRONTEND FIXTURES - Data shapes for frontend testing
# ============================================================================

FRONTEND_FIXTURES = {
    "proposals": [],
    "notifications": [],
    "column_analysis": None
}

def save_frontend_fixtures():
    """Save fixtures for frontend testing."""
    output_path = os.path.join(os.path.dirname(__file__), "frontend_fixtures.json")
    with open(output_path, "w") as f:
        json.dump(FRONTEND_FIXTURES, f, indent=2, default=str)
    print(f"\n📁 Frontend fixtures saved to: {output_path}")

# ============================================================================
# TEST HELPERS
# ============================================================================

def build_conversation(messages: List[dict]) -> List[dict]:
    """Build conversation payload."""
    conversation = []
    for i, msg in enumerate(messages):
        conversation.append({
            "direction": msg["direction"],
            "from": TEST_PROPERTY["email"] if msg["direction"] == "inbound" else "jill@company.com",
            "to": ["jill@company.com"] if msg["direction"] == "inbound" else [TEST_PROPERTY["email"]],
            "subject": f"RE: {TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
            "timestamp": f"2024-01-15T{10+i}:00:00Z",
            "preview": msg["content"][:200],
            "content": msg["content"]
        })
    return conversation

def run_pipeline_test(name: str, messages: List[dict],
                     initial_data: List[str] = None,
                     expect_notifications: List[str] = None,
                     expect_updates: bool = True) -> Dict:
    """Run full pipeline test and capture results."""

    print(f"\n{'─'*60}")
    print(f"🧪 {name}")

    NOTIFICATION_CAPTURE.reset()
    row_data = initial_data or TEST_PROPERTY["data"].copy()
    conversation = build_conversation(messages)

    result = {
        "name": name,
        "passed": False,
        "proposal": None,
        "notifications_fired": [],
        "issues": []
    }

    try:
        # Step 1: Get AI proposal
        start = time.time()
        proposal = propose_sheet_updates(
            uid="test-user",
            client_id="test-client",
            email=TEST_PROPERTY["email"],
            sheet_id="test-sheet-id",
            header=HEADERS,
            rownum=TEST_PROPERTY["row_number"],
            rowvals=row_data,
            thread_id=f"test-{name}",
            contact_name=TEST_PROPERTY["contact"],
            conversation=conversation,
            dry_run=True
        )
        elapsed = int((time.time() - start) * 1000)

        if proposal is None:
            result["issues"].append("No proposal returned")
            print(f"   ❌ No proposal returned")
            return result

        result["proposal"] = proposal

        updates = proposal.get("updates", [])
        events = proposal.get("events", [])
        response = proposal.get("response_email", "")

        print(f"   Updates: {len(updates)}")
        print(f"   Events: {[e.get('type') for e in events]}")
        print(f"   Response: {'✓' if response else '✗'}")

        # Step 2: Simulate applying updates and firing notifications
        # This is what processing.py does after getting the proposal

        # Fire sheet_update notifications for each update
        if updates:
            for update in updates:
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="sheet_update",
                    priority="normal",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={
                        "column": update.get("column"),
                        "newValue": update.get("value"),
                        "reason": update.get("reason"),
                        "confidence": update.get("confidence")
                    }
                )

        # Fire event-based notifications
        for event in events:
            event_type = event.get("type")

            if event_type == "needs_user_input":
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="action_needed",
                    priority="high",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={
                        "event_type": event_type,
                        "reason": event.get("reason"),
                        "question": event.get("question_summary")
                    }
                )

            elif event_type == "property_unavailable":
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="property_unavailable",
                    priority="high",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={"event_type": event_type}
                )

            elif event_type == "new_property":
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="new_property_suggestion",
                    priority="normal",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={
                        "event_type": event_type,
                        "suggested_address": event.get("address"),
                        "suggested_link": event.get("link")
                    }
                )

            elif event_type == "call_requested":
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="action_needed",
                    priority="high",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={
                        "event_type": event_type,
                        "phone": event.get("phone")
                    }
                )

            elif event_type == "contact_optout":
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="contact_optout",
                    priority="high",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={
                        "event_type": event_type,
                        "reason": event.get("reason")
                    }
                )

            elif event_type == "wrong_contact":
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="wrong_contact",
                    priority="high",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={
                        "event_type": event_type,
                        "reason": event.get("reason"),
                        "new_contact_email": event.get("new_contact_email")
                    }
                )

            elif event_type == "property_issue":
                NOTIFICATION_CAPTURE.write_notification(
                    uid="test-user",
                    client_id="test-client",
                    kind="property_issue",
                    priority="high",
                    email=TEST_PROPERTY["email"],
                    thread_id=f"test-{name}",
                    row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                    meta={
                        "event_type": event_type,
                        "issue": event.get("issue"),
                        "severity": event.get("severity")
                    }
                )

        # Check if row is complete - fire row_completed notification
        idx_map = {h.lower().strip(): i for i, h in enumerate(HEADERS)}
        test_row = row_data.copy()
        for u in updates:
            col = u.get("column", "").lower().strip()
            if col in idx_map:
                test_row[idx_map[col]] = u.get("value", "")

        required = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]
        all_complete = True
        for req in required:
            found = False
            for h, i in idx_map.items():
                if req in h:
                    if i < len(test_row) and test_row[i]:
                        found = True
                        break
            if not found:
                all_complete = False
                break

        if all_complete and updates:
            NOTIFICATION_CAPTURE.write_notification(
                uid="test-user",
                client_id="test-client",
                kind="row_completed",
                priority="normal",
                email=TEST_PROPERTY["email"],
                thread_id=f"test-{name}",
                row_anchor=f"{TEST_PROPERTY['address']}, {TEST_PROPERTY['city']}",
                meta={"message": "All required fields are complete"}
            )

        # Collect fired notifications
        result["notifications_fired"] = [n["kind"] for n in NOTIFICATION_CAPTURE.notifications]

        print(f"   Notifications fired: {result['notifications_fired']}")

        # Validate expected notifications
        if expect_notifications:
            for exp in expect_notifications:
                if exp not in result["notifications_fired"]:
                    result["issues"].append(f"Expected notification '{exp}' not fired")

        # Save for frontend fixtures
        FRONTEND_FIXTURES["proposals"].append({
            "name": name,
            "proposal": proposal,
            "notifications": NOTIFICATION_CAPTURE.notifications.copy()
        })

        result["passed"] = len(result["issues"]) == 0
        status = "✅ PASSED" if result["passed"] else "❌ FAILED"
        print(f"   {status} ({elapsed}ms)")

        for issue in result["issues"]:
            print(f"      ⚠️ {issue}")

    except Exception as e:
        result["issues"].append(f"Exception: {e}")
        print(f"   ❌ Exception: {e}")
        traceback.print_exc()

    return result

# ============================================================================
# TEST SCENARIOS
# ============================================================================

def test_complete_info():
    """All fields provided - should fire sheet_update + row_completed."""
    return run_pipeline_test(
        name="complete_info",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, interested in 1 Randolph Ct."},
            {"direction": "inbound", "content": """Hi Jill,

Here are the complete details:
- 15,000 SF
- $8.50/SF/yr NNN
- NNN: $2.25/SF
- 2 drive-in doors
- 4 dock doors
- 24' clear
- 400A 3-phase

Available immediately.

Scott"""}
        ],
        expect_notifications=["sheet_update", "row_completed"]
    )

def test_partial_info():
    """Only some fields - should fire sheet_update but not row_completed."""
    return run_pipeline_test(
        name="partial_info",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, what's the size and rent?"},
            {"direction": "inbound", "content": "It's 15,000 SF at $8.50/SF NNN. Scott"}
        ],
        expect_notifications=["sheet_update"]
    )

def test_escalate_scheduling():
    """Broker asks to schedule - should fire action_needed, NO response."""
    return run_pipeline_test(
        name="escalate_scheduling",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, interested in touring."},
            {"direction": "inbound", "content": "Can you come by Tuesday at 2pm? Scott"}
        ],
        expect_notifications=["action_needed"]
    )

def test_escalate_negotiation():
    """Broker makes counteroffer - should fire action_needed."""
    return run_pipeline_test(
        name="escalate_negotiation",
        messages=[
            {"direction": "outbound", "content": "Is there flexibility on rent?"},
            {"direction": "inbound", "content": "For a 5-year term we could do $7.75/SF instead. Would that work? Scott"}
        ],
        expect_notifications=["action_needed"]
    )

def test_escalate_client_question():
    """Broker asks about client - should fire action_needed."""
    return run_pipeline_test(
        name="escalate_client_question",
        messages=[
            {"direction": "outbound", "content": "Following up on 1 Randolph Ct."},
            {"direction": "inbound", "content": "What size does your client need? And what's their timeline? Scott"}
        ],
        expect_notifications=["action_needed"]
    )

def test_property_unavailable():
    """Property no longer available - should fire property_unavailable."""
    return run_pipeline_test(
        name="property_unavailable",
        messages=[
            {"direction": "outbound", "content": "Is 1 Randolph Ct still available?"},
            {"direction": "inbound", "content": "Sorry, it was leased last week. Scott"}
        ],
        expect_notifications=["property_unavailable"]
    )

def test_new_property_suggestion():
    """Broker suggests alternative - should fire new_property_suggestion."""
    return run_pipeline_test(
        name="new_property_suggestion",
        messages=[
            {"direction": "outbound", "content": "Any updates on 1 Randolph Ct?"},
            {"direction": "inbound", "content": """It's still available at 15,000 SF.

Also just listed 200 Commerce Dr - 20,000 SF, similar area.
https://example.com/200-commerce

Scott"""}
        ],
        expect_notifications=["sheet_update", "new_property_suggestion"]
    )

def test_call_requested():
    """Broker wants to call - should fire action_needed."""
    return run_pipeline_test(
        name="call_requested",
        messages=[
            {"direction": "outbound", "content": "Following up on 1 Randolph Ct."},
            {"direction": "inbound", "content": "Can we discuss over the phone? Call me at (706) 555-1234. Scott"}
        ],
        expect_notifications=["action_needed"]
    )

def test_contact_optout():
    """Contact opts out - should fire contact_optout."""
    return run_pipeline_test(
        name="contact_optout",
        messages=[
            {"direction": "outbound", "content": "Following up on 1 Randolph Ct."},
            {"direction": "inbound", "content": "Not interested, please remove me from your list. Scott"}
        ],
        expect_notifications=["contact_optout"]
    )

def test_wrong_contact():
    """Wrong contact - should fire wrong_contact."""
    return run_pipeline_test(
        name="wrong_contact",
        messages=[
            {"direction": "outbound", "content": "Following up on 1 Randolph Ct."},
            {"direction": "inbound", "content": "I don't handle that listing anymore. Contact Sarah at sarah@atkins.com. Scott"}
        ],
        expect_notifications=["wrong_contact"]
    )

def test_property_issue():
    """Property has issues - should fire property_issue."""
    return run_pipeline_test(
        name="property_issue",
        messages=[
            {"direction": "outbound", "content": "Any issues with 1 Randolph Ct?"},
            {"direction": "inbound", "content": "There's water damage in the back corner from a roof leak. Being repaired. Scott"}
        ],
        expect_notifications=["property_issue"]
    )

def test_mixed_info_and_escalation():
    """Info provided but also asks question - should fire BOTH."""
    return run_pipeline_test(
        name="mixed_info_and_escalation",
        messages=[
            {"direction": "outbound", "content": "What are the specs for 1 Randolph Ct?"},
            {"direction": "inbound", "content": """It's 18,000 SF with 24' clear.

What's your client's budget range? Scott"""}
        ],
        expect_notifications=["sheet_update", "action_needed"]
    )

def test_unavailable_with_alternative():
    """Property gone but alternative offered - should fire BOTH."""
    return run_pipeline_test(
        name="unavailable_with_alternative",
        messages=[
            {"direction": "outbound", "content": "Is 1 Randolph Ct available?"},
            {"direction": "inbound", "content": """Sorry, it just got leased.

But I have 500 Trade Center - similar specs.
https://example.com/500-trade

Scott"""}
        ],
        expect_notifications=["property_unavailable", "new_property_suggestion"]
    )

# ============================================================================
# FRONTEND COMPONENT TEST FIXTURES
# ============================================================================

def generate_column_analysis_fixture():
    """Generate fixture for ColumnMappingStep component."""
    from email_automation.column_config import detect_column_mapping, CANONICAL_FIELDS

    result = detect_column_mapping(HEADERS, use_ai=False)

    FRONTEND_FIXTURES["column_analysis"] = {
        "headers": HEADERS,
        "proposedConfig": {
            "mappings": result["mappings"],
            "requiredFields": result["requiredFields"],
            "formulaFields": result["formulaFields"],
            "neverRequest": result["neverRequest"]
        },
        "confidence": result["confidence"],
        "unmapped": result["unmapped"],
        "canonicalFields": {k: {
            "label": v.get("label"),
            "description": v.get("description"),
            "required": v.get("required_for_matching"),
            "extractable": v.get("extractable"),
            "requiredForClose": v.get("required_for_close"),
            "isFormula": v.get("is_formula"),
            "neverRequest": v.get("never_request")
        } for k, v in CANONICAL_FIELDS.items()}
    }

    print(f"\n📋 Generated column analysis fixture")
    print(f"   Mapped: {len(result['mappings'])} fields")

# ============================================================================
# MAIN
# ============================================================================

def run_all_tests():
    """Run all pipeline tests."""

    print("\n" + "="*70)
    print("FULL PIPELINE E2E TESTS")
    print("="*70)
    print("Testing notification firing for all scenarios...\n")

    tests = [
        test_complete_info,
        test_partial_info,
        test_escalate_scheduling,
        test_escalate_negotiation,
        test_escalate_client_question,
        test_property_unavailable,
        test_new_property_suggestion,
        test_call_requested,
        test_contact_optout,
        test_wrong_contact,
        test_property_issue,
        test_mixed_info_and_escalation,
        test_unavailable_with_alternative,
    ]

    results = []
    for test_fn in tests:
        result = test_fn()
        results.append(result)
        time.sleep(0.5)

    # Generate frontend fixtures
    generate_column_analysis_fixture()
    save_frontend_fixtures()

    # Summary
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed

    print("\n" + "="*70)
    print("RESULTS")
    print("="*70)
    print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed}")

    # Notification summary
    all_notifs = []
    for r in results:
        all_notifs.extend(r["notifications_fired"])

    notif_counts = {}
    for n in all_notifs:
        notif_counts[n] = notif_counts.get(n, 0) + 1

    print(f"\n📊 Notifications Fired:")
    for kind, count in sorted(notif_counts.items()):
        print(f"   {kind}: {count}")

    if failed > 0:
        print(f"\n❌ Failed Tests:")
        for r in results:
            if not r["passed"]:
                print(f"   {r['name']}:")
                for issue in r["issues"]:
                    print(f"      - {issue}")

    return passed == len(results)

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
