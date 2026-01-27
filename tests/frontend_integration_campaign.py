#!/usr/bin/env python3
"""
Frontend Integration Campaign Test
===================================

This tests all frontend interaction scenarios by running campaigns that exercise
every type of notification, modal, and UI state the frontend needs to handle.

Frontend Areas Tested:
1. NotificationsSidebar - All notification types appear correctly
2. NewPropertyRequestModal - New property suggestions with contact info
3. UserInputModal - Identity, budget, negotiation questions
4. TourRequestModal - Tour scheduling workflow
5. SheetViewer - Row updates and completion states
6. CampaignProgress - Campaign completion detection

Usage:
    python tests/frontend_integration_campaign.py
"""

import os
import sys
import json
from datetime import datetime
from typing import Dict, List, Any
from dataclasses import dataclass, field

# Environment setup
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

for var in ["AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY"]:
    if not os.getenv(var):
        os.environ[var] = f"test-{var.lower()}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock Firestore
from unittest.mock import MagicMock
mock_firestore = MagicMock()
mock_firestore.Client = MagicMock(return_value=MagicMock())
mock_firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
sys.modules['google.cloud.firestore'] = mock_firestore
sys.modules['google.cloud'] = MagicMock()
sys.modules['google.oauth2.credentials'] = MagicMock()
sys.modules['google.auth.transport.requests'] = MagicMock()
sys.modules['googleapiclient.discovery'] = MagicMock()

from email_automation.ai_processing import propose_sheet_updates


HEADER = [
    "Property Address", "City", "Property Name", "Leasing Company",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments", "Flyer / Link", "Floorplan", "Jill and Clients comments"
]


@dataclass
class FrontendTestCase:
    """A test case for frontend interaction."""
    name: str
    description: str
    frontend_component: str  # Which frontend component this tests
    property_address: str
    broker_response: str
    expected_notification_kind: str
    expected_modal: str = None
    expected_sheet_update: bool = False
    expected_event_type: str = None
    contact: str = "Test Broker"
    email: str = "broker@test.com"


FRONTEND_TEST_CASES = [
    # =========================================================================
    # NOTIFICATION SIDEBAR TESTS
    # =========================================================================
    FrontendTestCase(
        name="sheet_update_notification",
        description="Basic field extraction triggers sheet_update notification",
        frontend_component="NotificationsSidebar",
        property_address="101 Sidebar Test Ave",
        broker_response="""The property is 15,000 SF at $7.50/SF NNN.

Best,
Broker""",
        expected_notification_kind="sheet_update",
        expected_sheet_update=True
    ),

    FrontendTestCase(
        name="row_completed_notification",
        description="All fields complete triggers row_completed notification",
        frontend_component="NotificationsSidebar",
        property_address="102 Sidebar Test Ave",
        broker_response="""Here's everything for 102 Sidebar Test Ave:
- 20,000 SF
- $8.00/SF NNN
- OpEx: $2.10/SF
- 2 drive-ins, 3 dock doors
- 26' clear height
- 400A 3-phase power

Thanks!""",
        expected_notification_kind="row_completed",
        expected_sheet_update=True
    ),

    FrontendTestCase(
        name="property_unavailable_notification",
        description="Unavailable property triggers property_unavailable notification",
        frontend_component="NotificationsSidebar",
        property_address="103 Sidebar Test Ave",
        broker_response="""Sorry, 103 Sidebar Test Ave was leased last week.

Broker""",
        expected_notification_kind="property_unavailable",
        expected_event_type="property_unavailable"
    ),

    FrontendTestCase(
        name="action_needed_notification",
        description="User input needed triggers action_needed notification",
        frontend_component="NotificationsSidebar",
        property_address="104 Sidebar Test Ave",
        broker_response="""Who is this inquiry for? I need to know the company name.

Broker""",
        expected_notification_kind="action_needed",
        expected_event_type="needs_user_input"
    ),

    # =========================================================================
    # NEW PROPERTY REQUEST MODAL TESTS
    # =========================================================================
    FrontendTestCase(
        name="new_property_same_contact",
        description="Broker suggests new property - triggers NewPropertyRequestModal",
        frontend_component="NewPropertyRequestModal",
        property_address="201 Modal Test St",
        broker_response="""201 Modal Test St is no longer available, but I have 250 Modal Test St
which is similar - 18,000 SF, $7.25/SF. Here's the flyer: https://example.com/250-modal

Broker""",
        expected_notification_kind="action_needed",
        expected_modal="NewPropertyRequestModal",
        expected_event_type="new_property"
    ),

    FrontendTestCase(
        name="new_property_different_contact",
        description="Broker suggests property with different contact - modal shows new contact",
        frontend_component="NewPropertyRequestModal",
        property_address="202 Modal Test St",
        broker_response="""I don't handle 202 Modal Test St anymore. Contact Sarah at sarah@other.com
for that one. She also has 260 Modal Test St which might work for you.

John""",
        expected_notification_kind="action_needed",
        expected_modal="NewPropertyRequestModal",
        expected_event_type="new_property"
    ),

    # =========================================================================
    # USER INPUT MODAL TESTS
    # =========================================================================
    FrontendTestCase(
        name="identity_question_modal",
        description="Identity question triggers UserInputModal with confidential context",
        frontend_component="UserInputModal",
        property_address="301 Modal Test Blvd",
        broker_response="""Before I provide details, can you tell me who your client is?
What company are they with?

Broker""",
        expected_notification_kind="action_needed",
        expected_modal="UserInputModal",
        expected_event_type="needs_user_input"
    ),

    FrontendTestCase(
        name="budget_question_modal",
        description="Budget question triggers UserInputModal with client_question context",
        frontend_component="UserInputModal",
        property_address="302 Modal Test Blvd",
        broker_response="""The space at 302 Modal Test Blvd is 25,000 SF.

What's your client's budget range? That will help me know if this is a fit.

Broker""",
        expected_notification_kind="action_needed",
        expected_modal="UserInputModal",
        expected_event_type="needs_user_input"
    ),

    FrontendTestCase(
        name="requirements_question_modal",
        description="Space requirements question triggers UserInputModal",
        frontend_component="UserInputModal",
        property_address="303 Modal Test Blvd",
        broker_response="""Happy to help with 303 Modal Test Blvd.

What are the specific requirements? Do they need rail access? Hazmat storage?

Broker""",
        expected_notification_kind="action_needed",
        expected_modal="UserInputModal",
        expected_event_type="needs_user_input"
    ),

    FrontendTestCase(
        name="negotiation_modal",
        description="Counteroffer triggers UserInputModal with negotiation context",
        frontend_component="UserInputModal",
        property_address="304 Modal Test Blvd",
        broker_response="""For 304 Modal Test Blvd, the owner won't go below $8.50/SF.

However, if your client commits to a 5-year term, we could do $7.75/SF. Would that work?

Broker""",
        expected_notification_kind="action_needed",
        expected_modal="UserInputModal",
        expected_event_type="needs_user_input"
    ),

    # =========================================================================
    # TOUR REQUEST MODAL TESTS
    # =========================================================================
    FrontendTestCase(
        name="tour_offered_modal",
        description="Tour offer triggers TourRequestModal",
        frontend_component="TourRequestModal",
        property_address="401 Tour Test Dr",
        broker_response="""401 Tour Test Dr is 22,000 SF at $7.00/SF NNN.

Would you like to schedule a tour? I'm available Tuesday at 2pm or Thursday morning.

Broker""",
        expected_notification_kind="action_needed",
        expected_modal="TourRequestModal",
        expected_event_type="tour_requested"
    ),

    FrontendTestCase(
        name="showing_offered_modal",
        description="Showing offer (alternate wording) triggers TourRequestModal",
        frontend_component="TourRequestModal",
        property_address="402 Tour Test Dr",
        broker_response="""Happy to show you 402 Tour Test Dr. Let me know when works for you.

Broker""",
        expected_notification_kind="action_needed",
        expected_modal="TourRequestModal",
        expected_event_type="tour_requested"
    ),

    # =========================================================================
    # CALL REQUESTED TESTS
    # =========================================================================
    FrontendTestCase(
        name="call_requested_notification",
        description="Call request triggers action_needed with phone number",
        frontend_component="NotificationsSidebar",
        property_address="501 Call Test Way",
        broker_response="""There's a lot to discuss about 501 Call Test Way - complex TI situation.

Can you call me at 555-123-4567? Easier to explain by phone.

Broker""",
        expected_notification_kind="action_needed",
        expected_event_type="call_requested"
    ),

    # =========================================================================
    # CONTACT OPT-OUT TESTS
    # =========================================================================
    FrontendTestCase(
        name="contact_optout_notification",
        description="Contact opt-out triggers appropriate notification",
        frontend_component="NotificationsSidebar",
        property_address="601 Optout Test Ln",
        broker_response="""Please remove me from your mailing list. We don't work with tenant reps.

-John""",
        expected_notification_kind="action_needed",
        expected_event_type="contact_optout"
    ),

    # =========================================================================
    # SHEET STATE TESTS
    # =========================================================================
    FrontendTestCase(
        name="partial_update_progress",
        description="Partial info shows row in progress state",
        frontend_component="SheetViewer",
        property_address="701 Sheet Test Ct",
        broker_response="""701 Sheet Test Ct is 16,000 SF at $6.75/SF.

Broker""",
        expected_notification_kind="sheet_update",
        expected_sheet_update=True
    ),

    FrontendTestCase(
        name="multi_field_update",
        description="Multiple fields updated in single response",
        frontend_component="SheetViewer",
        property_address="702 Sheet Test Ct",
        broker_response="""Details for 702 Sheet Test Ct:
- 19,000 SF
- $7.25/SF NNN
- NNN: $1.95/SF
- 2 drive-ins, 4 docks

Still need to confirm clear height and power.

Broker""",
        expected_notification_kind="sheet_update",
        expected_sheet_update=True
    ),
]


def run_frontend_test(test_case: FrontendTestCase) -> Dict[str, Any]:
    """Run a single frontend integration test."""

    # Build conversation
    conversation = [{
        "direction": "outbound",
        "content": f"Hi, I'm interested in {test_case.property_address}. Could you provide details?",
        "from": "jill@mohrpartners.com",
        "to": [test_case.email],
        "subject": f"{test_case.property_address}",
        "timestamp": "2026-01-25T10:00:00Z"
    }, {
        "direction": "inbound",
        "content": test_case.broker_response,
        "from": test_case.email,
        "to": ["jill@mohrpartners.com"],
        "subject": f"Re: {test_case.property_address}",
        "timestamp": "2026-01-25T11:00:00Z"
    }]

    # Build row values
    rowvals = [""] * len(HEADER)
    rowvals[HEADER.index("Property Address")] = test_case.property_address
    rowvals[HEADER.index("City")] = "Augusta"
    rowvals[HEADER.index("Leasing Contact")] = test_case.contact
    rowvals[HEADER.index("Email")] = test_case.email

    # Call AI
    try:
        proposal = propose_sheet_updates(
            uid="frontend-test-user",
            client_id="frontend-test-client",
            email=test_case.email,
            sheet_id="frontend-test-sheet",
            header=HEADER,
            rownum=2,
            rowvals=rowvals,
            thread_id=f"thread-frontend-{test_case.name}",
            contact_name=test_case.contact,
            conversation=conversation,
            dry_run=True
        )

        return {
            "test_name": test_case.name,
            "component": test_case.frontend_component,
            "updates": proposal.get("updates", []) if proposal else [],
            "events": proposal.get("events", []) if proposal else [],
            "response_email": proposal.get("response_email") if proposal else None,
            "notes": proposal.get("notes", "") if proposal else "",
            "success": True
        }
    except Exception as e:
        return {
            "test_name": test_case.name,
            "component": test_case.frontend_component,
            "error": str(e),
            "success": False
        }


def derive_notifications(result: Dict) -> List[Dict]:
    """Derive what notifications would be sent to frontend."""
    notifications = []

    # Sheet updates
    for update in result.get("updates", []):
        notifications.append({
            "kind": "sheet_update",
            "meta": {"column": update["column"], "value": update["value"]}
        })

    # Events -> notifications
    for event in result.get("events", []):
        event_type = event.get("type")

        if event_type == "property_unavailable":
            notifications.append({"kind": "property_unavailable"})

        elif event_type == "needs_user_input":
            notifications.append({
                "kind": "action_needed",
                "meta": {
                    "reason": f"needs_user_input:{event.get('reason', 'unknown')}",
                    "question": event.get("question", "")
                }
            })

        elif event_type == "call_requested":
            notifications.append({
                "kind": "action_needed",
                "meta": {"reason": "call_requested"}
            })

        elif event_type == "tour_requested":
            notifications.append({
                "kind": "action_needed",
                "meta": {
                    "reason": "tour_requested",
                    "question": event.get("question", "")
                }
            })

        elif event_type == "new_property":
            notifications.append({
                "kind": "action_needed",
                "meta": {
                    "reason": "new_property_pending_approval",
                    "address": event.get("address"),
                    "email": event.get("email"),
                    "contactName": event.get("contactName")
                }
            })

        elif event_type == "contact_optout":
            notifications.append({
                "kind": "action_needed",
                "meta": {"reason": "contact_optout"}
            })

    # Check for row completed (all required fields)
    required = {"Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"}
    updated_cols = {u["column"] for u in result.get("updates", [])}
    if required.issubset(updated_cols):
        notifications.append({"kind": "row_completed"})

    return notifications


def validate_test(test_case: FrontendTestCase, result: Dict) -> Dict[str, Any]:
    """Validate a test result against expectations."""
    notifications = derive_notifications(result)
    events = result.get("events", [])

    validation = {
        "passed": True,
        "checks": []
    }

    # Check 1: Expected notification kind present
    notification_kinds = {n["kind"] for n in notifications}
    has_expected_kind = test_case.expected_notification_kind in notification_kinds

    validation["checks"].append({
        "name": "Expected notification kind",
        "expected": test_case.expected_notification_kind,
        "actual": list(notification_kinds),
        "passed": has_expected_kind
    })
    if not has_expected_kind:
        validation["passed"] = False

    # Check 2: Expected event type (if specified)
    if test_case.expected_event_type:
        event_types = {e.get("type") for e in events}
        has_expected_event = test_case.expected_event_type in event_types

        validation["checks"].append({
            "name": "Expected event type",
            "expected": test_case.expected_event_type,
            "actual": list(event_types),
            "passed": has_expected_event
        })
        if not has_expected_event:
            validation["passed"] = False

    # Check 3: Sheet updates (if expected)
    if test_case.expected_sheet_update:
        has_updates = len(result.get("updates", [])) > 0

        validation["checks"].append({
            "name": "Sheet updates generated",
            "expected": True,
            "actual": has_updates,
            "passed": has_updates
        })
        if not has_updates:
            validation["passed"] = False

    return validation


def run_all_frontend_tests(verbose: bool = True) -> Dict[str, Any]:
    """Run all frontend integration tests."""

    if verbose:
        print("\n" + "="*70)
        print("FRONTEND INTEGRATION CAMPAIGN TEST")
        print("="*70)
        print(f"Time: {datetime.now().isoformat()}")
        print(f"Test Cases: {len(FRONTEND_TEST_CASES)}")

    # Group by component
    by_component = {}
    for tc in FRONTEND_TEST_CASES:
        if tc.frontend_component not in by_component:
            by_component[tc.frontend_component] = []
        by_component[tc.frontend_component].append(tc)

    results = []
    passed = 0
    failed = 0

    for component, test_cases in by_component.items():
        if verbose:
            print(f"\n{'─'*70}")
            print(f"TESTING: {component}")
            print(f"{'─'*70}")

        for tc in test_cases:
            if verbose:
                print(f"\n  [{tc.name}]")
                print(f"  {tc.description}")

            result = run_frontend_test(tc)
            validation = validate_test(tc, result)

            result["validation"] = validation
            results.append(result)

            if validation["passed"]:
                passed += 1
                if verbose:
                    print(f"  Status: PASS")
            else:
                failed += 1
                if verbose:
                    print(f"  Status: FAIL")
                    for check in validation["checks"]:
                        if not check["passed"]:
                            print(f"    - {check['name']}: expected {check['expected']}, got {check['actual']}")

            # Show derived notifications for debugging
            if verbose and validation["passed"]:
                notifications = derive_notifications(result)
                notif_kinds = [n["kind"] for n in notifications]
                print(f"  Notifications: {notif_kinds}")

    # Summary
    if verbose:
        print(f"\n{'='*70}")
        print("FRONTEND INTEGRATION SUMMARY")
        print(f"{'='*70}")
        print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed}")
        print(f"Pass Rate: {passed/len(results)*100:.1f}%")

        print(f"\nBy Component:")
        for component in by_component.keys():
            comp_results = [r for r in results if r["component"] == component]
            comp_passed = sum(1 for r in comp_results if r["validation"]["passed"])
            status = "[OK]" if comp_passed == len(comp_results) else "[!!]"
            print(f"  {status} {component}: {comp_passed}/{len(comp_results)}")

    return {
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "pass_rate": passed/len(results) if results else 0,
        "by_component": {
            comp: {
                "total": len([r for r in results if r["component"] == comp]),
                "passed": sum(1 for r in results if r["component"] == comp and r["validation"]["passed"])
            }
            for comp in by_component.keys()
        },
        "results": results
    }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Frontend Integration Campaign Test")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--component", "-c", help="Test specific component only")
    args = parser.parse_args()

    summary = run_all_frontend_tests(not args.quiet)

    if summary["failed"] > 0:
        print(f"\n{summary['failed']} tests failed")
        return 1

    print(f"\nALL FRONTEND INTEGRATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
