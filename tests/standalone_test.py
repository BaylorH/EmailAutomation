#!/usr/bin/env python3
"""
Standalone Test Runner
======================
Tests the AI extraction logic by calling the PRODUCTION propose_sheet_updates() function.
This ensures tests exercise the exact same code path as real email processing.

Requires:
- OPENAI_API_KEY environment variable
- Firebase credentials (GOOGLE_APPLICATION_CREDENTIALS or default credentials)
- Azure env vars (can be dummy values since we use dry_run mode)
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

# ============================================================================
# ENVIRONMENT SETUP (must happen before importing production code)
# ============================================================================

# Load .env file if it exists
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

# Check for OpenAI API key (required for actual API calls)
if not os.getenv("OPENAI_API_KEY"):
    print("OPENAI_API_KEY environment variable not set")
    print("Please set it: export OPENAI_API_KEY='your-key' or add to .env file")
    sys.exit(1)

# Set dummy Azure env vars if not present (required for app_config import, but not used in dry_run)
if not os.getenv("AZURE_API_APP_ID"):
    os.environ["AZURE_API_APP_ID"] = "test-app-id"
if not os.getenv("AZURE_API_CLIENT_SECRET"):
    os.environ["AZURE_API_CLIENT_SECRET"] = "test-secret"
if not os.getenv("FIREBASE_API_KEY"):
    os.environ["FIREBASE_API_KEY"] = "test-firebase-key"

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock Firebase/Firestore before importing production code (avoids credential requirement)
from unittest.mock import MagicMock, patch
import sys as _sys

# Create mock for google.cloud.firestore
mock_firestore = MagicMock()
mock_firestore.Client = MagicMock(return_value=MagicMock())
mock_firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
_sys.modules['google.cloud.firestore'] = mock_firestore
_sys.modules['google.cloud'] = MagicMock()
_sys.modules['google.oauth2.credentials'] = MagicMock()
_sys.modules['google.auth.transport.requests'] = MagicMock()
_sys.modules['googleapiclient.discovery'] = MagicMock()

# Now import production code
try:
    from email_automation.ai_processing import propose_sheet_updates
    from email_automation.app_config import REQUIRED_FIELDS_FOR_CLOSE
    PRODUCTION_IMPORT_SUCCESS = True
except Exception as e:
    print(f"Warning: Could not import production code: {e}")
    print("Tests will use fallback mode (direct OpenAI calls)")
    PRODUCTION_IMPORT_SUCCESS = False
    REQUIRED_FIELDS_FOR_CLOSE = ["Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"]

# ============================================================================
# SHEET DATA (matches your real sheet structure)
# ============================================================================

HEADER = [
    "Property Address", "City", "Property Name", "Leasing Company",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments ", "Flyer / Link", "Floorplan",
    "Jill and Clients comments"
]

# Note: "Rent/SF /Yr" is never requested, "Gross Rent" is a formula column (never written or requested)
REQUIRED_FIELDS = REQUIRED_FIELDS_FOR_CLOSE

# Sample properties from your sheet
PROPERTIES = {
    "699 Industrial Park Dr": {
        "row": 3,
        "city": "Evans",
        "contact": "Jeff and Connie Wilson, CCIM",
        "email": "testing@gmail.com",
        "data": ["699 Industrial Park Dr", "Evans", "", "", "Jeff and Connie Wilson, CCIM,", "testing@gmail.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    "135 Trade Center Court": {
        "row": 4,
        "city": "Augusta",
        "contact": "Luke Coffey",
        "email": "testing2@gmail.com",
        "data": ["135 Trade Center Court", "Augusta", "", "", "Luke Coffey", "testing2@gmail.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    "1 Randolph Ct": {
        "row": 7,
        "city": "Evans",
        "contact": "Scott A. Atkins CCIM, SIOR",
        "email": "bp21harrison@gmail.com",
        "data": ["1 Randolph Ct", "Evans", "", "Atkins Commercial Properties", "Scott A. Atkins CCIM, SIOR", "bp21harrison@gmail.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    "2058 Gordon Hwy": {
        "row": 5,
        "city": "Augusta",
        "contact": "Jonathan Aceves",
        "email": "baylor@manifoldengineering.ai",
        "data": ["2058 Gordon Hwy", "Augusta", "Battery Clinic", "Meybohm Commercial Properties", "Jonathan Aceves", "baylor@manifoldengineering.ai", "", "", "", "", "", "", "", "", "", "", "", ""]
    }
}


def show_simulated_sheet_row(scenario_name: str, property_address: str, updates: List[Dict], header: List[str] = None):
    """
    Display a simulated sheet row showing before/after state.
    Shows only columns that have data or were updated.
    """
    if header is None:
        header = HEADER

    prop = PROPERTIES.get(property_address)
    if not prop:
        print(f"   Unknown property: {property_address}")
        return

    row_data = list(prop["data"])  # Copy to avoid mutation

    # Build column index map
    col_idx = {h.lower().strip(): i for i, h in enumerate(header)}

    # Apply updates
    updated_cols = set()
    for update in updates:
        col_name = update.get("column", "")
        value = update.get("value", "")
        idx = col_idx.get(col_name.lower().strip())
        if idx is not None and idx < len(row_data):
            updated_cols.add(idx)
            row_data[idx] = value

    # Display columns that have data
    print(f"\n   📊 SIMULATED SHEET ROW (after updates applied):")
    print(f"   " + "─" * 56)

    for i, (col, val) in enumerate(zip(header, row_data)):
        if val or i in updated_cols:
            marker = " ✏️" if i in updated_cols else ""
            val_display = val if val else "(empty)"
            print(f"   │ {col:25} │ {val_display:25} │{marker}")

    print(f"   " + "─" * 56)

    # Show which required fields are complete
    required = ["Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"]
    complete = []
    missing = []
    for req in required:
        idx = col_idx.get(req.lower().strip())
        if idx is not None and idx < len(row_data) and row_data[idx]:
            complete.append(req)
        else:
            missing.append(req)

    if complete:
        print(f"   ✅ Complete: {', '.join(complete)}")
    if missing:
        print(f"   ❌ Missing: {', '.join(missing)}")

    return row_data


def show_full_email_response(response_email: str, contact_name: str = ""):
    """Display the full email response with proper formatting."""
    if not response_email:
        print(f"\n   📧 RESPONSE EMAIL: (none - escalated to user)")
        return

    print(f"\n   📧 RESPONSE EMAIL:")
    print(f"   " + "─" * 56)
    # Add proper line breaks
    lines = response_email.split('\n')
    for line in lines:
        print(f"   │ {line}")
    # Show what would be appended (signature)
    print(f"   │ ")
    print(f"   │ Best,")
    print(f"   │ Jill")
    print(f"   " + "─" * 56)


@dataclass
class ExpectedNotification:
    """Expected notification definition."""
    kind: str  # "sheet_update", "action_needed", "property_unavailable", "row_completed"
    reason: str = None  # For action_needed: "call_requested", "needs_user_input:scheduling", etc.


@dataclass
class TestScenario:
    """A test scenario definition."""
    name: str
    description: str
    property_address: str
    messages: List[Dict]  # [{direction, content}]
    expected_updates: List[Dict]  # [{column, value}]
    expected_events: List[str]  # ["property_unavailable", "new_property", etc.]
    expected_response_type: str  # "missing_fields", "closing", "unavailable", etc.
    expected_notifications: List[ExpectedNotification] = None  # What notifications should fire
    forbidden_updates: List[str] = None  # Columns that should NEVER be updated (e.g., Leasing Contact)


@dataclass
class TestResult:
    """Result of running a test."""
    scenario_name: str
    passed: bool = False
    ai_updates: List[Dict] = field(default_factory=list)
    ai_events: List[Dict] = field(default_factory=list)
    ai_response: str = ""
    ai_notes: str = ""  # Captured notes for comments column
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    api_time_ms: int = 0
    derived_notifications: List[Dict] = field(default_factory=list)  # What notifications would fire


def derive_notifications(updates: List[Dict], events: List[Dict], row_data: List[str], header: List[str]) -> List[Dict]:
    """
    Derive what notifications WOULD fire based on AI results.
    This mirrors the logic in processing.py.
    """
    notifications = []

    # Each update triggers a sheet_update notification
    for update in updates:
        notifications.append({
            "kind": "sheet_update",
            "column": update.get("column"),
            "value": update.get("value")
        })

    # Process events
    for event in events:
        event_type = event.get("type")

        if event_type == "call_requested":
            notifications.append({
                "kind": "action_needed",
                "reason": "call_requested"
            })

        elif event_type == "needs_user_input":
            reason = event.get("reason", "unclear")
            notifications.append({
                "kind": "action_needed",
                "reason": f"needs_user_input:{reason}"
            })

        elif event_type == "property_unavailable":
            notifications.append({
                "kind": "property_unavailable"
            })

        elif event_type == "new_property":
            notifications.append({
                "kind": "action_needed",
                "reason": "new_property_pending_send"
            })

        elif event_type == "contact_optout":
            reason = event.get("reason", "not_interested")
            notifications.append({
                "kind": "action_needed",
                "reason": f"contact_optout:{reason}"
            })

        elif event_type == "wrong_contact":
            reason = event.get("reason", "wrong_person")
            notifications.append({
                "kind": "action_needed",
                "reason": f"wrong_contact:{reason}"
            })

        elif event_type == "tour_requested":
            notifications.append({
                "kind": "action_needed",
                "reason": "tour_requested"
            })

        elif event_type == "close_conversation":
            notifications.append({
                "kind": "conversation_closed",
                "reason": "natural_end"
            })

        elif event_type == "property_issue":
            severity = event.get("severity", "major")
            notifications.append({
                "kind": "action_needed",
                "reason": f"property_issue:{severity}"
            })

    # Check if all required fields would be complete after updates
    # Build current + updated values
    idx_map = {h.lower(): i for i, h in enumerate(header) if h}
    current_values = {h.lower(): row_data[i] if i < len(row_data) else "" for h, i in idx_map.items()}

    # Apply updates
    for update in updates:
        col = update.get("column", "").lower()
        val = update.get("value", "")
        if col:
            current_values[col] = val

    # Check required fields
    required = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]
    all_complete = all(current_values.get(f, "").strip() for f in required)

    if all_complete and updates:  # Only fire if we actually made updates
        notifications.append({
            "kind": "row_completed"
        })

    return notifications


# ============================================================================
# TEST SCENARIOS
# ============================================================================

SCENARIOS = [
    TestScenario(
        name="complete_info",
        description="Broker provides all required property information with extra details",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, I'm interested in 1 Randolph Ct in Evans. Could you provide the property details?"},
            {"direction": "inbound", "content": """Hi Jill,

Happy to help! Here are the details for 1 Randolph Ct:

- Total SF: 15,000
- Asking rent: $8.50/SF/yr NNN
- NNN/CAM: $2.25/SF/yr
- 2 drive-in doors
- 4 dock doors
- Clear height: 24 feet
- Power: 400 amps, 3-phase

The space is available immediately. It's a newer building, built in 2019, with a fenced yard and room for about 15 trailer parking spots. The landlord is motivated and flexible on lease terms - they'd consider anything from 3-5 years. The property is right off I-20 which is great for logistics.

Let me know if you need anything else!

Best,
Scott"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "15000"},
            {"column": "Rent/SF /Yr", "value": "8.50"},
            {"column": "Ops Ex /SF", "value": "2.25"},
            {"column": "Drive Ins", "value": "2"},
            {"column": "Docks", "value": "4"},
            {"column": "Ceiling Ht", "value": "24"},
            {"column": "Power", "value": "400 amps, 3-phase"},
        ],
        expected_events=[],
        expected_response_type="closing",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="sheet_update"),  # Rent/SF /Yr
            ExpectedNotification(kind="sheet_update"),  # Ops Ex /SF
            ExpectedNotification(kind="sheet_update"),  # Drive Ins
            ExpectedNotification(kind="sheet_update"),  # Docks
            ExpectedNotification(kind="sheet_update"),  # Ceiling Ht
            ExpectedNotification(kind="sheet_update"),  # Power
            ExpectedNotification(kind="row_completed"),  # All required fields complete
        ]
    ),

    TestScenario(
        name="partial_info",
        description="Broker provides only some fields - needs follow-up",
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, interested in 699 Industrial Park Dr. What are the details?"},
            {"direction": "inbound", "content": """Hi,

The space is 8,500 SF with asking rent of $6.00/SF NNN.

Jeff"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "8500"},
            {"column": "Rent/SF /Yr", "value": "6.00"},
        ],
        expected_events=[],
        expected_response_type="missing_fields",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="sheet_update"),  # Rent/SF /Yr
            # No row_completed - missing Ops Ex, Drive Ins, Docks, Ceiling Ht, Power
        ]
    ),

    TestScenario(
        name="property_unavailable",
        description="Broker says property is no longer available",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, following up on 135 Trade Center Court availability."},
            {"direction": "inbound", "content": """Hi Jill,

Unfortunately that property is no longer available - it was leased last week.

Luke"""}
        ],
        expected_updates=[],
        expected_events=["property_unavailable"],
        expected_response_type="unavailable",
        expected_notifications=[
            ExpectedNotification(kind="property_unavailable"),
        ]
    ),

    TestScenario(
        name="unavailable_with_alternative",
        description="Property unavailable but broker suggests alternative",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, is 1 Randolph Ct still available?"},
            {"direction": "inbound", "content": """Hi Jill,

Sorry, 1 Randolph Ct is no longer available - we just signed a lease yesterday.

However, I do have another property that might work for you:
456 Commerce Blvd in Martinez - similar size at around 12,000 SF.

Here's the listing: https://example.com/456-commerce

Let me know if you'd like details!

Scott"""}
        ],
        expected_updates=[],
        expected_events=["property_unavailable", "new_property"],
        expected_response_type="unavailable_with_alternative",
        expected_notifications=[
            ExpectedNotification(kind="property_unavailable"),
            ExpectedNotification(kind="action_needed", reason="new_property_pending_send"),
        ]
    ),

    TestScenario(
        name="call_requested_with_phone",
        description="Broker requests a call and provides phone number",
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, following up on 699 Industrial Park Dr."},
            {"direction": "inbound", "content": """Hi Jill,

I'd prefer to discuss this over the phone - there are some details that would be easier to explain.

Can you give me a call at (706) 555-1234?

Thanks,
Jeff"""}
        ],
        expected_updates=[],
        expected_events=["call_requested"],
        expected_response_type="call_with_phone",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="call_requested"),
        ]
    ),

    TestScenario(
        name="call_requested_no_phone",
        description="Broker requests a call but doesn't provide number",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, checking on 135 Trade Center Court availability."},
            {"direction": "inbound", "content": """Hi,

Can we schedule a call to discuss? I have several options that might work.

Luke"""}
        ],
        expected_updates=[],
        expected_events=["call_requested"],
        expected_response_type="ask_for_phone",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="call_requested"),
        ]
    ),

    TestScenario(
        name="multi_turn_conversation",
        description="Multiple exchanges gradually filling in data",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, interested in 1 Randolph Ct. What's the SF and rent?"},
            {"direction": "inbound", "content": "Hi Jill, it's 20,000 SF at $7.50/SF NNN. Scott"},
            {"direction": "outbound", "content": "Thanks! What are the NNN expenses and dock/door count?"},
            {"direction": "inbound", "content": """NNN is $1.85/SF.

We have 3 dock doors and 1 drive-in. Ceiling is 20' clear.

Scott"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "20000"},
            {"column": "Rent/SF /Yr", "value": "7.50"},
            {"column": "Ops Ex /SF", "value": "1.85"},
                        {"column": "Docks", "value": "3"},
            {"column": "Drive Ins", "value": "1"},
            {"column": "Ceiling Ht", "value": "20"},
        ],
        expected_events=[],
        expected_response_type="missing_fields",  # Still missing Power
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="sheet_update"),  # Rent/SF /Yr
            ExpectedNotification(kind="sheet_update"),  # Ops Ex /SF
            ExpectedNotification(kind="sheet_update"),  # Docks
            ExpectedNotification(kind="sheet_update"),  # Drive Ins
            ExpectedNotification(kind="sheet_update"),  # Ceiling Ht
            # No row_completed - missing Power
        ]
    ),

    TestScenario(
        name="vague_response",
        description="Broker gives vague response without concrete data",
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, what's the rent and SF for 699 Industrial Park Dr?"},
            {"direction": "inbound", "content": """Hi,

The rent is competitive for the area. It's a nice sized building with good loading.

Let me know if you want to tour.

Jeff"""}
        ],
        expected_updates=[],  # No concrete data to extract
        expected_events=[],
        expected_response_type="missing_fields",
        expected_notifications=[
            # No notifications - no data extracted, no significant events
            # Note: AI may fire needs_user_input:scheduling for tour offer (acceptable)
        ]
    ),

    TestScenario(
        name="new_property_suggestion",
        description="Broker proactively suggests additional property",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, any updates on 135 Trade Center Court?"},
            {"direction": "inbound", "content": """Hi Jill,

135 Trade Center is still available at 25,000 SF.

Also, we just got a new listing you might like:
200 Warehouse Way in North Augusta - 30,000 SF
https://example.com/200-warehouse-way

Both are good options for your client's needs.

Luke"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "25000"},
        ],
        expected_events=["new_property"],
        expected_response_type="missing_fields",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="action_needed", reason="new_property_pending_send"),
        ]
    ),

    TestScenario(
        name="close_conversation",
        description="Natural conversation conclusion",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Thanks for all the info on 135 Trade Center Court!"},
            {"direction": "inbound", "content": """You're welcome! Let me know if you need anything else. Good luck with your search!

Luke"""}
        ],
        expected_updates=[],
        expected_events=["close_conversation"],
        expected_response_type="closing",
        expected_notifications=[
            ExpectedNotification(kind="conversation_closed", reason="natural_end"),
        ]
    ),

    # ========================================================================
    # EDGE CASES: When AI should NOT auto-respond (escalate to user)
    # ========================================================================

    TestScenario(
        name="client_asks_requirements",
        description="Broker asks about client's space requirements - AI cannot answer",
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, I'm interested in 699 Industrial Park Dr. What are the details?"},
            {"direction": "inbound", "content": """Hi Jill,

Before I send over the details, what size space does your client need? And what's their timeline for moving in?

Thanks,
Jeff"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:client_question"),
        ]
    ),

    TestScenario(
        name="scheduling_request",
        description="Broker offers tour times - creates tour_requested notification with suggested email",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, interested in touring 1 Randolph Ct."},
            {"direction": "inbound", "content": """Hi Jill,

Great! Can you come by Tuesday at 2pm for a tour? Or would Wednesday morning work better?

Scott"""}
        ],
        expected_updates=[],
        expected_events=["tour_requested"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="tour_requested"),
        ]
    ),

    TestScenario(
        name="negotiation_attempt",
        description="Broker makes counteroffer - AI should not negotiate",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, is there flexibility on the $8.50/SF asking rent for 135 Trade Center Court?"},
            {"direction": "inbound", "content": """Hi Jill,

The landlord is firm at $8.50/SF, but if your client can commit to a 5-year term instead of 3, we could potentially do $7.75/SF. Would they consider that?

Luke"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:negotiation"),
        ]
    ),

    TestScenario(
        name="identity_question",
        description="Broker asks who the client is - AI should not reveal",
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, following up on 699 Industrial Park Dr."},
            {"direction": "inbound", "content": """Hi Jill,

Happy to help. Can you tell me who your client is? What company are they with and what do they do? We want to make sure it's a good fit for the building.

Jeff"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:confidential"),
        ]
    ),

    TestScenario(
        name="legal_contract_question",
        description="Broker asks about contract/LOI - AI cannot commit",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, the property looks good. What are the next steps?"},
            {"direction": "inbound", "content": """Hi Jill,

If your client is ready to move forward, can you send over an LOI with their proposed terms? We'd need the lease term, preferred start date, and any TI requirements.

Scott"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:legal_contract"),
        ]
    ),

    TestScenario(
        name="mixed_info_and_question",
        description="Broker provides info but also asks question requiring user input",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, what are the specs for 135 Trade Center Court?"},
            {"direction": "inbound", "content": """Hi Jill,

The space is 18,000 SF with 24' clear height. We have 3 docks and 1 drive-in.

By the way, what's your client's budget? And do they need heavy power or standard?

Luke"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "18000"},
            {"column": "Ceiling Ht", "value": "24"},
            {"column": "Docks", "value": "3"},
            {"column": "Drive Ins", "value": "1"},
        ],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="sheet_update"),  # Ceiling Ht
            ExpectedNotification(kind="sheet_update"),  # Docks
            ExpectedNotification(kind="sheet_update"),  # Drive Ins
            ExpectedNotification(kind="action_needed", reason="needs_user_input:client_question"),
        ]
    ),

    TestScenario(
        name="budget_question",
        description="Broker asks about budget - AI cannot answer",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, can you send details on 1 Randolph Ct?"},
            {"direction": "inbound", "content": """Hi Jill,

Sure thing. Before I do, what's the budget range your client is working with? Just want to make sure this is in their price range.

Scott"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:client_question"),
        ]
    ),

    TestScenario(
        name="different_person_replies",
        description="Different person replies but Leasing Contact should NOT be updated",
        property_address="2058 Gordon Hwy",  # Jonathan Aceves is the contact
        messages=[
            {"direction": "outbound", "content": "Hi Jonathan, I'm interested in 2058 Gordon Hwy. Can you provide property details?"},
            {"direction": "inbound", "content": """Hey yeah I can help ya on this property.

The space is 10,000 SF with $6.50/SF NNN asking rent.

Thanks,
Baylor Harrison"""}  # Different person signing!
        ],
        expected_updates=[
            {"column": "Total SF", "value": "10000"},
            {"column": "Rent/SF /Yr", "value": "6.50"},
            # CRITICAL: NO Leasing Contact update!
        ],
        forbidden_updates=["Leasing Contact", "Leasing Company", "Email"],  # These should NEVER be updated
        expected_events=[],
        expected_response_type="missing_fields",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="sheet_update"),  # Rent/SF /Yr
        ]
    ),

    TestScenario(
        name="new_property_suggestion_with_different_contact",
        description="Broker suggests new property with different contact - should NOT update original Leasing Contact",
        property_address="2058 Gordon Hwy",
        messages=[
            {"direction": "outbound", "content": "Hi Jonathan, is 2058 Gordon Hwy still available?"},
            {"direction": "inbound", "content": """Hey yeah I can help ya on this property but here's another property reach out to Baylor bp21harrison@gmail.com about 435 sicko street

Thanks,
Someone Else"""}
        ],
        expected_updates=[],  # No updates to original property - they didn't provide specs
        forbidden_updates=["Leasing Contact", "Leasing Company"],  # NEVER update these
        expected_events=["new_property"],
        expected_response_type="new_property",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="new_property_pending_send"),
        ]
    ),

    # ========================================================================
    # CONTACT OPT-OUT SCENARIOS
    # ========================================================================

    TestScenario(
        name="contact_optout_not_interested",
        description="Contact explicitly says not interested",
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, I'm reaching out about 699 Industrial Park Dr."},
            {"direction": "inbound", "content": """Not interested, thanks.

Jeff"""}
        ],
        expected_updates=[],
        expected_events=["contact_optout"],
        expected_response_type="escalate",  # No auto-response to opted-out contacts
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="contact_optout:not_interested"),
        ]
    ),

    TestScenario(
        name="contact_optout_no_tenant_reps",
        description="Contact says they don't work with tenant reps",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, inquiring about 135 Trade Center Court."},
            {"direction": "inbound", "content": """Hi,

We don't work with tenant rep brokers. Please remove us from your list.

Thanks,
Luke"""}
        ],
        expected_updates=[],
        expected_events=["contact_optout"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="contact_optout:no_tenant_reps"),
        ]
    ),

    # ========================================================================
    # WRONG CONTACT SCENARIOS
    # ========================================================================

    TestScenario(
        name="wrong_contact_redirected",
        description="Contact says they no longer handle property and redirects to someone else",
        property_address="2058 Gordon Hwy",
        messages=[
            {"direction": "outbound", "content": "Hi Jonathan, is 2058 Gordon Hwy still available?"},
            {"direction": "inbound", "content": """Hi Jill,

I don't handle that property anymore. You should reach out to Sarah Johnson at sarah.johnson@cbre.com - she took over our industrial portfolio.

Best,
Jonathan"""}
        ],
        expected_updates=[],
        expected_events=["wrong_contact"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="wrong_contact:no_longer_handles"),
        ]
    ),

    TestScenario(
        name="wrong_contact_left_company",
        description="Contact has left the company",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, following up on 1 Randolph Ct."},
            {"direction": "inbound", "content": """Hi,

Scott no longer works here. He left the company last month.
Try reaching out to our main office at info@broker.com.

Thanks,
Reception"""}
        ],
        expected_updates=[],
        expected_events=["wrong_contact"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="wrong_contact:left_company"),
        ]
    ),

    # ========================================================================
    # PROPERTY ISSUE SCENARIOS
    # ========================================================================

    TestScenario(
        name="property_issue_major",
        description="Broker mentions significant property issue",
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, what are the specs for 699 Industrial Park Dr?"},
            {"direction": "inbound", "content": """Hi Jill,

The property is 15,000 SF at $5.50/SF NNN. Clear height is 20ft.

FYI - there's been some water damage in the northeast corner that the landlord is getting quotes to repair. The HVAC is also original from 1992.

Let me know if you still want to proceed.

Jeff"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "15000"},
            {"column": "Rent/SF /Yr", "value": "5.50"},
            {"column": "Ceiling Ht", "value": "20"},
        ],
        expected_events=["property_issue"],
        expected_response_type="missing_fields",  # Still asks for remaining fields
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="sheet_update"),  # Rent
            ExpectedNotification(kind="sheet_update"),  # Ceiling Ht
            ExpectedNotification(kind="action_needed", reason="property_issue:major"),
        ]
    ),

    TestScenario(
        name="property_issue_critical",
        description="Broker mentions critical health/safety issue",
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, can you send details on 135 Trade Center Court?"},
            {"direction": "inbound", "content": """Hi Jill,

Just want to give you a heads up - the building has asbestos that would need professional abatement before occupancy. Cost estimates have been around $50-75k.

The space is 20,000 SF otherwise nice.

Luke"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "20000"},
        ],
        expected_events=["property_issue"],
        expected_response_type="missing_fields",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),  # Total SF
            ExpectedNotification(kind="action_needed", reason="property_issue:critical"),
        ]
    ),
]


def build_conversation(scenario: TestScenario) -> list[dict]:
    """
    Build a conversation payload in the format expected by propose_sheet_updates().
    This matches the output of build_conversation_payload() from messaging.py.
    """
    prop = PROPERTIES.get(scenario.property_address)
    if not prop:
        raise ValueError(f"Unknown property: {scenario.property_address}")

    target_anchor = f"{scenario.property_address}, {prop['city']}"

    conversation = []
    for i, msg in enumerate(scenario.messages):
        conversation.append({
            "direction": msg["direction"],
            "from": prop["email"] if msg["direction"] == "inbound" else "jill@company.com",
            "to": ["jill@company.com"] if msg["direction"] == "inbound" else [prop["email"]],
            "subject": target_anchor,
            "timestamp": f"2024-01-15T{10+i}:00:00Z",
            "preview": msg["content"][:200],
            "content": msg["content"]
        })

    return conversation


def call_production_function(scenario: TestScenario) -> Tuple[Optional[dict], int]:
    """
    Call the production propose_sheet_updates() function.
    Returns (proposal_dict, elapsed_ms).
    """
    prop = PROPERTIES.get(scenario.property_address)
    if not prop:
        return {"error": f"Unknown property: {scenario.property_address}"}, 0

    conversation = build_conversation(scenario)

    start = time.time()
    try:
        proposal = propose_sheet_updates(
            uid="test-user",
            client_id="test-client",
            email=prop["email"],
            sheet_id="test-sheet-id",
            header=HEADER,
            rownum=prop["row"],
            rowvals=prop["data"],
            thread_id=f"test-thread-{scenario.name}",
            contact_name=prop["contact"],
            conversation=conversation,  # Pass conversation directly (bypasses Firestore)
            dry_run=True  # Skip Firestore logging
        )
        elapsed = int((time.time() - start) * 1000)

        if proposal is None:
            return {"error": "propose_sheet_updates returned None"}, elapsed

        return proposal, elapsed

    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        return {"error": str(e)}, elapsed


def validate_result(scenario: TestScenario, result: Dict, row_data: List[str] = None) -> tuple:
    """Validate result against expectations. Returns (passed, issues, warnings, derived_notifications)."""
    issues = []
    warnings = []

    updates = result.get("updates", [])
    events = result.get("events", [])
    response = result.get("response_email", "")

    # Check updates
    actual_updates = {u["column"]: u["value"] for u in updates}
    for exp in scenario.expected_updates:
        col = exp["column"]
        exp_val = str(exp["value"]).replace(",", "")

        if col not in actual_updates:
            issues.append(f"Missing update: {col}")
            continue

        act_val = str(actual_updates[col]).replace(",", "")
        if exp_val != act_val:
            issues.append(f"Wrong value for {col}: expected '{exp_val}', got '{act_val}'")

    # Check events
    actual_event_types = {e.get("type") for e in events}
    expected_event_types = set(scenario.expected_events)

    missing_events = expected_event_types - actual_event_types
    if missing_events:
        issues.append(f"Missing events: {missing_events}")

    extra_events = actual_event_types - expected_event_types
    if extra_events:
        warnings.append(f"Extra events: {extra_events}")

    # Check response email doesn't request forbidden fields
    if response:
        response_lower = response.lower()
        if "rent/sf /yr" in response_lower or "rent/sf/yr" in response_lower:
            issues.append("Response email requests 'Rent/SF /Yr' (FORBIDDEN)")
        if "gross rent" in response_lower:
            issues.append("Response email requests 'Gross Rent' (FORBIDDEN - it's a formula)")

    # Check AI didn't try to write to Gross Rent
    for u in updates:
        if u.get("column", "").lower() == "gross rent":
            issues.append("AI tried to write to 'Gross Rent' (FORBIDDEN - it's a formula column)")

    # Check AI didn't try to write to forbidden columns (like Leasing Contact)
    if scenario.forbidden_updates:
        for u in updates:
            col = u.get("column", "")
            if col in scenario.forbidden_updates:
                issues.append(f"AI tried to update '{col}' (FORBIDDEN - pre-existing client data that should NEVER be changed)")

    # Check escalation scenarios
    if scenario.expected_response_type == "escalate":
        # When escalating, AI should emit needs_user_input OR tour_requested and NOT generate a response
        escalation_events = {"needs_user_input", "tour_requested"}
        if not (escalation_events & set(actual_event_types)):
            issues.append("Expected 'needs_user_input' or 'tour_requested' event for escalation scenario")

        # Response should be null/empty when escalating
        if response and response.strip():
            issues.append("Response email should be null/empty when escalating to user")

        # Check that needs_user_input event has required fields
        for e in events:
            if e.get("type") == "needs_user_input":
                if not e.get("reason"):
                    warnings.append("needs_user_input event missing 'reason' field")
                if not e.get("question"):
                    warnings.append("needs_user_input event missing 'question' field")
            elif e.get("type") == "tour_requested":
                if not e.get("question"):
                    warnings.append("tour_requested event missing 'question' field")

    # Check that response_email is present for non-escalation scenarios (except call_requested with phone)
    elif scenario.expected_response_type not in ["escalate", "call_with_phone"]:
        if "needs_user_input" in actual_event_types:
            # AI escalated when it shouldn't have
            warnings.append("AI escalated to user when it could have responded automatically")

    # ========================================================================
    # NOTIFICATION VALIDATION
    # ========================================================================
    derived_notifications = []
    if row_data is not None and scenario.expected_notifications is not None:
        # Derive what notifications would fire
        derived_notifications = derive_notifications(updates, events, row_data, HEADER)

        # Build comparable sets
        expected_notifs = scenario.expected_notifications or []

        # Count expected notifications by kind
        expected_counts = {}
        for en in expected_notifs:
            key = (en.kind, en.reason)
            expected_counts[key] = expected_counts.get(key, 0) + 1

        # Count derived notifications by kind
        derived_counts = {}
        for dn in derived_notifications:
            key = (dn["kind"], dn.get("reason"))
            derived_counts[key] = derived_counts.get(key, 0) + 1

        # Check for missing notifications
        for key, count in expected_counts.items():
            kind, reason = key
            derived_count = derived_counts.get(key, 0)
            if derived_count < count:
                reason_str = f" ({reason})" if reason else ""
                issues.append(f"Missing notification: {kind}{reason_str} (expected {count}, got {derived_count})")

        # Check for unexpected notifications (as warnings, not failures)
        for key, count in derived_counts.items():
            kind, reason = key
            expected_count = expected_counts.get(key, 0)
            if count > expected_count:
                reason_str = f" ({reason})" if reason else ""
                # row_completed is okay as extra if fields complete
                if kind == "row_completed" and expected_count == 0:
                    warnings.append(f"Extra notification: {kind}{reason_str} (row may have completed)")
                elif kind == "action_needed" and reason and reason.startswith("needs_user_input:"):
                    # Extra escalation is a warning, not failure
                    warnings.append(f"Extra escalation notification: {kind}{reason_str}")
                else:
                    warnings.append(f"Extra notification: {kind}{reason_str}")

        # Validate needs_user_input reason matches expectation
        for en in expected_notifs:
            if en.kind == "action_needed" and en.reason and en.reason.startswith("needs_user_input:"):
                expected_subreason = en.reason.split(":")[1]
                found_match = False
                for dn in derived_notifications:
                    if dn["kind"] == "action_needed" and dn.get("reason", "").startswith("needs_user_input:"):
                        derived_subreason = dn["reason"].split(":")[1]
                        if derived_subreason == expected_subreason:
                            found_match = True
                            break
                        # Allow some flexibility in reason classification
                        # client_question and confidential are both about client info
                        if expected_subreason in ["client_question", "confidential"] and derived_subreason in ["client_question", "confidential"]:
                            found_match = True
                            warnings.append(f"Notification reason mismatch: expected '{expected_subreason}', got '{derived_subreason}' (acceptable)")
                            break
                if not found_match:
                    # Check if ANY needs_user_input was fired (partial match)
                    any_nui = any(dn["kind"] == "action_needed" and dn.get("reason", "").startswith("needs_user_input:") for dn in derived_notifications)
                    if any_nui:
                        actual_reasons = [dn["reason"] for dn in derived_notifications if dn["kind"] == "action_needed" and dn.get("reason", "").startswith("needs_user_input:")]
                        warnings.append(f"Notification reason mismatch: expected 'needs_user_input:{expected_subreason}', got {actual_reasons}")
                    # Don't add as issue since event was already validated above

    passed = len(issues) == 0
    return passed, issues, warnings, derived_notifications


def run_test(scenario: TestScenario, verbose: bool = True) -> TestResult:
    """Run a single test scenario."""
    result = TestResult(scenario_name=scenario.name)

    if verbose:
        print(f"\n{'='*60}")
        print(f"Test: {scenario.name}")
        print(f"   {scenario.description}")
        print(f"   Property: {scenario.property_address}")
        print(f"{'='*60}")

        print("\n   Conversation:")
        for msg in scenario.messages:
            arrow = "   ->" if msg["direction"] == "outbound" else "   <-"
            preview = msg["content"][:60].replace('\n', ' ')
            print(f"   {arrow} {preview}...")

    # Call production function
    if verbose:
        mode = "PRODUCTION" if PRODUCTION_IMPORT_SUCCESS else "FALLBACK"
        print(f"\n   Calling OpenAI via {mode} code path...")

    ai_result, elapsed = call_production_function(scenario)
    result.api_time_ms = elapsed

    if "error" in ai_result:
        result.issues.append(f"API Error: {ai_result['error']}")
        if verbose:
            print(f"   Error: {ai_result['error']}")
        return result

    result.ai_updates = ai_result.get("updates", [])
    result.ai_events = ai_result.get("events", [])
    result.ai_response = ai_result.get("response_email", "")
    result.ai_notes = ai_result.get("notes", "")

    if verbose:
        print(f"\n   Response ({elapsed}ms):")
        print(f"   Updates: {len(result.ai_updates)}")
        for u in result.ai_updates:
            print(f"      - {u.get('column')}: {u.get('value')} (conf: {u.get('confidence', 'N/A')})")

        print(f"   Events: {[e.get('type') for e in result.ai_events]}")

        # Show details for needs_user_input events
        for e in result.ai_events:
            if e.get("type") == "needs_user_input":
                print(f"   Escalation: reason={e.get('reason', 'N/A')}")
                if e.get("question"):
                    print(f"      Question: {e.get('question')[:80]}...")

        if result.ai_response:
            preview = result.ai_response[:80].replace('\n', ' ')
            print(f"   Response email: {preview}...")
        else:
            print(f"   Response email: (none - escalated to user)")

        if result.ai_notes:
            print(f"   Notes: {result.ai_notes}")

        # Get property data for display
        prop = PROPERTIES.get(scenario.property_address)

        # Show simulated sheet row (visual representation)
        show_simulated_sheet_row(scenario.name, scenario.property_address, result.ai_updates)

        # Show full email response
        show_full_email_response(result.ai_response, prop["contact"] if prop else "")

    # Get row data for notification validation (prop may already be set if verbose)
    if not verbose:
        prop = PROPERTIES.get(scenario.property_address)
    row_data = prop["data"] if prop else []

    # Validate
    passed, issues, warnings, derived_notifications = validate_result(scenario, ai_result, row_data)
    result.passed = passed
    result.issues = issues
    result.warnings = warnings
    result.derived_notifications = derived_notifications

    if verbose:
        # Show derived notifications
        if derived_notifications:
            print(f"\n   Notifications that would fire:")
            for dn in derived_notifications:
                reason_str = f" ({dn['reason']})" if dn.get('reason') else ""
                if dn['kind'] == 'sheet_update':
                    print(f"      - sheet_update: {dn.get('column')} = {dn.get('value')}")
                else:
                    print(f"      - {dn['kind']}{reason_str}")
        else:
            print(f"\n   Notifications: (none)")

        print(f"\n   {'PASS' if passed else 'FAIL'}")
        for i in issues:
            print(f"      - {i}")
        for w in warnings:
            print(f"      Warning: {w}")

    return result


def run_all(verbose: bool = True) -> List[TestResult]:
    """Run all test scenarios."""
    print("\n" + "="*70)
    print("EMAIL AUTOMATION AI TEST SUITE")
    print("="*70)
    mode = "PRODUCTION CODE PATH" if PRODUCTION_IMPORT_SUCCESS else "FALLBACK MODE"
    print(f"Mode: {mode}")
    print(f"Running {len(SCENARIOS)} scenarios...")

    results = []
    for scenario in SCENARIOS:
        result = run_test(scenario, verbose=verbose)
        results.append(result)
        time.sleep(0.3)  # Small delay between API calls

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed}")
    print(f"Pass Rate: {passed/len(results)*100:.1f}%")

    avg_time = sum(r.api_time_ms for r in results) / len(results)
    print(f"Avg API Time: {avg_time:.0f}ms")

    if failed > 0:
        print("\nFailed tests:")
        for r in results:
            if not r.passed:
                print(f"\n   {r.scenario_name}:")
                for i in r.issues:
                    print(f"      - {i}")

    return results


def save_report(results: List[TestResult], filename: str = "test_results.json"):
    """Save test results to JSON file."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "production" if PRODUCTION_IMPORT_SUCCESS else "fallback",
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "results": []
    }

    for r in results:
        report["results"].append({
            "name": r.scenario_name,
            "passed": r.passed,
            "api_time_ms": r.api_time_ms,
            "updates": r.ai_updates,
            "events": r.ai_events,
            "response": r.ai_response,
            "notes": r.ai_notes,
            "issues": r.issues,
            "warnings": r.warnings
        })

    with open(filename, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\nReport saved to: {filename}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Email Automation AI Tests")
    parser.add_argument("-s", "--scenario", help="Run specific scenario by name")
    parser.add_argument("-l", "--list", action="store_true", help="List scenarios")
    parser.add_argument("-q", "--quiet", action="store_true", help="Less output")
    parser.add_argument("-r", "--report", help="Save report to file")

    args = parser.parse_args()

    if args.list:
        print("\nAvailable scenarios:")
        for s in SCENARIOS:
            print(f"  - {s.name}: {s.description}")
    elif args.scenario:
        scenario = next((s for s in SCENARIOS if s.name == args.scenario), None)
        if scenario:
            result = run_test(scenario, verbose=not args.quiet)
        else:
            print(f"Scenario '{args.scenario}' not found")
    else:
        results = run_all(verbose=not args.quiet)
        if args.report:
            save_report(results, args.report)
