#!/usr/bin/env python3
"""
End-to-End Test Suite
=====================
Comprehensive tests using the actual Excel template structure.
Tests the full flow: conversation → AI extraction → sheet update → notification.

This suite validates:
1. Column mapping flexibility (order/naming)
2. All conversation scenarios
3. Notification firing for all event types
4. Required field completion detection
5. Edge cases and error handling

Usage:
    export OPENAI_API_KEY='your-key'
    python tests/e2e_test_suite.py
    python tests/e2e_test_suite.py -s scenario_name
    python tests/e2e_test_suite.py --category escalation
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from enum import Enum

# ============================================================================
# ENVIRONMENT SETUP
# ============================================================================

if not os.getenv("OPENAI_API_KEY"):
    print("OPENAI_API_KEY environment variable not set")
    sys.exit(1)

# Set dummy env vars for imports
for var in ["AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY"]:
    if not os.getenv(var):
        os.environ[var] = f"test-{var.lower()}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock Firebase before imports
from unittest.mock import MagicMock
mock_firestore = MagicMock()
mock_firestore.Client = MagicMock(return_value=MagicMock())
mock_firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
sys.modules['google.cloud.firestore'] = mock_firestore
sys.modules['google.cloud'] = MagicMock()
sys.modules['google.oauth2.credentials'] = MagicMock()
sys.modules['google.auth.transport.requests'] = MagicMock()
sys.modules['googleapiclient.discovery'] = MagicMock()

# Import production code
from email_automation.ai_processing import propose_sheet_updates, check_missing_required_fields
from email_automation.column_config import (
    detect_column_mapping,
    get_default_column_config,
    build_column_rules_prompt,
    CANONICAL_FIELDS,
    REQUIRED_FOR_CLOSE,
)

# ============================================================================
# SHEET TEMPLATES (from Excel "Scrub Augusta GA.xlsx")
# ============================================================================

# Standard header from the Excel file
STANDARD_HEADER = [
    "Property Address", "City", "Property Name", "Leasing Company",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments ", "Flyer / Link", "Floorplan",
    "Jill and Clients comments"
]

# Alternative headers to test column flexibility
ALT_HEADER_RENAMED = [
    "Address", "City", "Building Name", "Brokerage",
    "Contact Name", "Email Address", "Square Footage", "Asking Rent", "NNN/CAM",
    "Monthly Rent", "Drive-In Doors", "Loading Docks", "Clear Height", "Electrical",
    "Broker Notes", "Links", "Floor Plans", "Internal Notes"
]

ALT_HEADER_REORDERED = [
    "City", "Property Address", "Email", "Leasing Contact",
    "Total SF", "Rent/SF /Yr", "Ceiling Ht", "Docks", "Drive Ins",
    "Power", "Ops Ex /SF", "Gross Rent", "Property Name", "Leasing Company",
    "Flyer / Link", "Listing Brokers Comments ", "Floorplan", "Jill and Clients comments"
]

# Sample properties from the Excel
PROPERTIES = {
    "699 Industrial Park Dr": {
        "row": 3,
        "city": "Evans",
        "contact": "Jeff and Connie Wilson, CCIM",
        "email": "jeff@wilsonrealty.com",
        "data": ["699 Industrial Park Dr", "Evans", "", "", "Jeff and Connie Wilson, CCIM,", "jeff@wilsonrealty.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    "135 Trade Center Court": {
        "row": 4,
        "city": "Augusta",
        "contact": "Luke Coffey",
        "email": "luke@augustabrokers.com",
        "data": ["135 Trade Center Court", "Augusta", "", "", "Luke Coffey", "luke@augustabrokers.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    "2058 Gordon Hwy": {
        "row": 5,
        "city": "Augusta",
        "contact": "Jonathan Aceves",
        "email": "jonathan@meybohm.com",
        "data": ["2058 Gordon Hwy", "Augusta", "Battery Clinic", "Meybohm Commercial Properties", "Jonathan Aceves", "jonathan@meybohm.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    "1 Kuhlke Dr": {
        "row": 6,
        "city": "Augusta",
        "contact": "Robert McCrary",
        "email": "robert@mccrary.com",
        "data": ["1 Kuhlke Dr", "Augusta", "", "", "Robert McCrary", "robert@mccrary.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    "1 Randolph Ct": {
        "row": 7,
        "city": "Evans",
        "contact": "Scott A. Atkins CCIM, SIOR",
        "email": "scott@atkinscommercial.com",
        "data": ["1 Randolph Ct", "Evans", "", "Atkins Commercial Properties", "Scott A. Atkins CCIM, SIOR", "scott@atkinscommercial.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
}


# ============================================================================
# TEST CATEGORIES
# ============================================================================

class TestCategory(Enum):
    EXTRACTION = "extraction"      # Field extraction from conversations
    ESCALATION = "escalation"      # When AI should NOT auto-respond
    EVENTS = "events"              # Special events (unavailable, new property, etc)
    NOTIFICATIONS = "notifications" # Notification firing validation
    COLUMN_FLEX = "column_flex"    # Column naming/ordering flexibility
    EDGE_CASES = "edge_cases"      # Error handling and edge cases


@dataclass
class ExpectedNotification:
    kind: str  # "sheet_update", "action_needed", "property_unavailable", "row_completed"
    reason: str = None
    column: str = None


@dataclass
class TestScenario:
    name: str
    description: str
    category: TestCategory
    property_address: str
    messages: List[Dict]
    expected_updates: List[Dict]
    expected_events: List[str]
    expected_response_type: str  # "closing", "missing_fields", "escalate", "unavailable", etc
    expected_notifications: List[ExpectedNotification] = None
    header: List[str] = None  # Optional custom header for column flexibility tests
    column_config: Dict = None  # Optional custom column config
    initial_row_data: List[str] = None  # Optional pre-filled row data


@dataclass
class TestResult:
    scenario_name: str
    category: str
    passed: bool = False
    ai_updates: List[Dict] = field(default_factory=list)
    ai_events: List[Dict] = field(default_factory=list)
    ai_response: str = ""
    ai_notes: str = ""
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    api_time_ms: int = 0
    derived_notifications: List[Dict] = field(default_factory=list)


# ============================================================================
# TEST SCENARIOS
# ============================================================================

SCENARIOS = [
    # -------------------------------------------------------------------------
    # EXTRACTION: Field extraction from conversations
    # -------------------------------------------------------------------------
    TestScenario(
        name="extract_all_fields",
        description="Broker provides all property details in one message",
        category=TestCategory.EXTRACTION,
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, I'm interested in 1 Randolph Ct. Can you send the property details?"},
            {"direction": "inbound", "content": """Hi Jill,

Here are the details for 1 Randolph Ct:

- Total SF: 15,000
- Rent: $8.50/SF/yr NNN
- NNN: $2.25/SF/yr
- 2 drive-in doors
- 4 dock doors
- 24' clear height
- 400A 3-phase power

Available immediately. Built 2019, fenced yard, 15 trailer spots. Landlord is flexible on 3-5 year terms.

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
            {"column": "Power", "value": "400A 3-phase"},
        ],
        expected_events=[],
        expected_response_type="closing",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),
            ExpectedNotification(kind="row_completed"),
        ]
    ),

    TestScenario(
        name="extract_partial_fields",
        description="Broker provides only some fields - missing info needs follow-up",
        category=TestCategory.EXTRACTION,
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, interested in 699 Industrial Park Dr. What are the specs?"},
            {"direction": "inbound", "content": """Hi,

It's 8,500 SF at $6.00/SF NNN.

Jeff"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "8500"},
            {"column": "Rent/SF /Yr", "value": "6.00"},
        ],
        expected_events=[],
        expected_response_type="missing_fields",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),
        ]
    ),

    TestScenario(
        name="extract_multi_turn",
        description="Information gathered across multiple conversation turns",
        category=TestCategory.EXTRACTION,
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, what's the SF and rent for 135 Trade Center Court?"},
            {"direction": "inbound", "content": "Hi Jill, it's 20,000 SF at $7.50/SF NNN. Luke"},
            {"direction": "outbound", "content": "Thanks! What are the NNN expenses and loading?"},
            {"direction": "inbound", "content": """NNN is $1.85/SF.

3 dock doors and 1 drive-in. 20' clear height.

Luke"""}
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
        expected_response_type="missing_fields",  # Missing Power
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),
        ]
    ),

    TestScenario(
        name="extract_with_notes",
        description="Capture additional details in notes field",
        category=TestCategory.EXTRACTION,
        property_address="2058 Gordon Hwy",
        messages=[
            {"direction": "outbound", "content": "Hi Jonathan, can you tell me about 2058 Gordon Hwy?"},
            {"direction": "inbound", "content": """Hi Jill,

Here's the info:
- 12,000 SF
- $5.50/SF NNN
- NNN is $1.50/SF
- 2 docks, 1 drive-in
- 18' clear
- 200A power

The property is zoned M-1 heavy industrial. ESFR sprinklered. Climate controlled office. Near I-20 exit. Can subdivide down to 6,000 SF minimum.

Available March 1st with flexible 3-7 year terms.

Jonathan"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "12000"},
            {"column": "Rent/SF /Yr", "value": "5.50"},
            {"column": "Ops Ex /SF", "value": "1.50"},
            {"column": "Docks", "value": "2"},
            {"column": "Drive Ins", "value": "1"},
            {"column": "Ceiling Ht", "value": "18"},
            {"column": "Power", "value": "200A"},
        ],
        expected_events=[],
        expected_response_type="closing",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),
            ExpectedNotification(kind="row_completed"),
        ]
    ),

    # -------------------------------------------------------------------------
    # ESCALATION: When AI should NOT auto-respond
    # -------------------------------------------------------------------------
    TestScenario(
        name="escalate_client_requirements",
        description="Broker asks about client's space requirements",
        category=TestCategory.ESCALATION,
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, interested in 699 Industrial Park Dr."},
            {"direction": "inbound", "content": """Hi Jill,

Before I send details, what size does your client need? And what's their timeline for moving in?

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
        name="escalate_scheduling",
        description="Broker wants to schedule a tour",
        category=TestCategory.ESCALATION,
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, interested in touring 1 Randolph Ct."},
            {"direction": "inbound", "content": """Hi Jill,

Can you come by Tuesday at 2pm? Or would Wednesday morning work better?

Scott"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:scheduling"),
        ]
    ),

    TestScenario(
        name="escalate_negotiation",
        description="Broker makes counteroffer on rent",
        category=TestCategory.ESCALATION,
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, is there flexibility on the $8.50/SF rent?"},
            {"direction": "inbound", "content": """Hi Jill,

The landlord is firm at $8.50/SF, but for a 5-year term instead of 3, we could do $7.75/SF. Would your client consider that?

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
        name="escalate_client_identity",
        description="Broker asks who the client is",
        category=TestCategory.ESCALATION,
        property_address="2058 Gordon Hwy",
        messages=[
            {"direction": "outbound", "content": "Hi Jonathan, following up on 2058 Gordon Hwy."},
            {"direction": "inbound", "content": """Hi Jill,

Who is your client? What company are they with and what do they do? We want to make sure it's a good fit.

Jonathan"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:confidential"),
        ]
    ),

    TestScenario(
        name="escalate_legal_contract",
        description="Broker asks about LOI/contract",
        category=TestCategory.ESCALATION,
        property_address="1 Kuhlke Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Robert, the property looks good. What are next steps?"},
            {"direction": "inbound", "content": """Hi Jill,

If your client is ready, can you send an LOI with proposed terms? We'd need lease term, start date, and TI requirements.

Robert"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="needs_user_input:legal_contract"),
        ]
    ),

    TestScenario(
        name="escalate_mixed_info_question",
        description="Broker provides info but also asks question requiring user input",
        category=TestCategory.ESCALATION,
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, what are the specs for 135 Trade Center Court?"},
            {"direction": "inbound", "content": """Hi Jill,

The space is 18,000 SF with 24' clear height. 3 docks and 1 drive-in.

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
            ExpectedNotification(kind="sheet_update"),
            ExpectedNotification(kind="action_needed", reason="needs_user_input:client_question"),
        ]
    ),

    # -------------------------------------------------------------------------
    # EVENTS: Special events detection
    # -------------------------------------------------------------------------
    TestScenario(
        name="event_property_unavailable",
        description="Property is no longer available",
        category=TestCategory.EVENTS,
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
        name="event_unavailable_with_alternative",
        description="Property unavailable but broker suggests alternative",
        category=TestCategory.EVENTS,
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, is 1 Randolph Ct still available?"},
            {"direction": "inbound", "content": """Hi Jill,

Sorry, 1 Randolph Ct just got leased yesterday.

However, I have another property that might work:
456 Commerce Blvd in Martinez - similar size around 12,000 SF.
https://example.com/456-commerce

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
        name="event_new_property_suggestion",
        description="Broker proactively suggests additional property",
        category=TestCategory.EVENTS,
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, any updates on 699 Industrial Park Dr?"},
            {"direction": "inbound", "content": """Hi Jill,

699 Industrial Park is still available at 8,500 SF.

Also, we just got a new listing you might like:
200 Warehouse Way in North Augusta - 15,000 SF
https://example.com/200-warehouse

Both could work for your client.

Jeff"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "8500"},
        ],
        expected_events=["new_property"],
        expected_response_type="missing_fields",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),
            ExpectedNotification(kind="action_needed", reason="new_property_pending_send"),
        ]
    ),

    TestScenario(
        name="event_call_requested_with_phone",
        description="Broker requests a call and provides phone number",
        category=TestCategory.EVENTS,
        property_address="2058 Gordon Hwy",
        messages=[
            {"direction": "outbound", "content": "Hi Jonathan, following up on 2058 Gordon Hwy."},
            {"direction": "inbound", "content": """Hi Jill,

I'd prefer to discuss this over the phone - some details are easier to explain.

Can you call me at (706) 555-1234?

Jonathan"""}
        ],
        expected_updates=[],
        expected_events=["call_requested"],
        expected_response_type="call_with_phone",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="call_requested"),
        ]
    ),

    TestScenario(
        name="event_call_requested_no_phone",
        description="Broker requests a call without providing number",
        category=TestCategory.EVENTS,
        property_address="1 Kuhlke Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Robert, checking on 1 Kuhlke Dr availability."},
            {"direction": "inbound", "content": """Hi,

Can we schedule a call to discuss? I have several options that might work.

Robert"""}
        ],
        expected_updates=[],
        expected_events=["call_requested"],
        expected_response_type="ask_for_phone",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="call_requested"),
        ]
    ),

    TestScenario(
        name="event_contact_optout",
        description="Broker says not interested / unsubscribe",
        category=TestCategory.EVENTS,
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, following up on 699 Industrial Park Dr."},
            {"direction": "inbound", "content": """Not interested, please remove me from your list.

Jeff"""}
        ],
        expected_updates=[],
        expected_events=["contact_optout"],
        expected_response_type="optout",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="contact_optout:not_interested"),
        ]
    ),

    TestScenario(
        name="event_wrong_contact",
        description="Broker says they're not the right contact",
        category=TestCategory.EVENTS,
        property_address="135 Trade Center Court",
        messages=[
            {"direction": "outbound", "content": "Hi Luke, following up on 135 Trade Center Court."},
            {"direction": "inbound", "content": """Hi Jill,

I don't handle that property anymore. Please contact Sarah Johnson at sarah@augustabrokers.com - she took over that listing.

Luke"""}
        ],
        expected_updates=[],
        expected_events=["wrong_contact"],
        expected_response_type="wrong_contact",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="wrong_contact:no_longer_handles"),
        ]
    ),

    TestScenario(
        name="event_property_issue",
        description="Broker mentions a problem with the property",
        category=TestCategory.EVENTS,
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, any issues I should know about with 1 Randolph Ct?"},
            {"direction": "inbound", "content": """Hi Jill,

The building has some water damage in the back corner that needs repair. Landlord is working on it but wanted to give you a heads up.

Scott"""}
        ],
        expected_updates=[],
        expected_events=["property_issue"],
        expected_response_type="acknowledge_issue",
        expected_notifications=[
            ExpectedNotification(kind="action_needed", reason="property_issue"),
        ]
    ),

    TestScenario(
        name="event_close_conversation",
        description="Natural conversation conclusion",
        category=TestCategory.EVENTS,
        property_address="2058 Gordon Hwy",
        messages=[
            {"direction": "outbound", "content": "Thanks for all the info on 2058 Gordon Hwy!"},
            {"direction": "inbound", "content": """You're welcome! Let me know if you need anything else. Good luck with your search!

Jonathan"""}
        ],
        expected_updates=[],
        expected_events=["close_conversation"],
        expected_response_type="closing",
        expected_notifications=[]  # close_conversation does NOT create notification
    ),

    # -------------------------------------------------------------------------
    # COLUMN FLEXIBILITY: Test different column names and orders
    # -------------------------------------------------------------------------
    TestScenario(
        name="column_flex_renamed",
        description="Test extraction with renamed columns",
        category=TestCategory.COLUMN_FLEX,
        property_address="1 Randolph Ct",
        header=ALT_HEADER_RENAMED,
        messages=[
            {"direction": "outbound", "content": "Hi Scott, what are the specs for 1 Randolph Ct?"},
            {"direction": "inbound", "content": """Hi Jill,

- 15,000 square feet
- $8.50/SF asking
- NNN is $2.25/SF
- 4 loading docks
- 2 drive-ins
- 24 foot clear
- 400 amps 3-phase

Scott"""}
        ],
        expected_updates=[
            {"column": "Square Footage", "value": "15000"},
            {"column": "Asking Rent", "value": "8.50"},
            {"column": "NNN/CAM", "value": "2.25"},
            {"column": "Loading Docks", "value": "4"},
            {"column": "Drive-In Doors", "value": "2"},
            {"column": "Clear Height", "value": "24"},
            {"column": "Electrical", "value": "400 amps 3-phase"},
        ],
        expected_events=[],
        expected_response_type="closing",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),
            ExpectedNotification(kind="row_completed"),
        ]
    ),

    # -------------------------------------------------------------------------
    # EDGE CASES: Error handling and unusual situations
    # -------------------------------------------------------------------------
    TestScenario(
        name="edge_vague_response",
        description="Broker gives vague response with no concrete data",
        category=TestCategory.EDGE_CASES,
        property_address="1 Kuhlke Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Robert, what's the rent and SF for 1 Kuhlke Dr?"},
            {"direction": "inbound", "content": """Hi,

The rent is competitive for the area. Nice sized building with good loading.

Let me know if you want to tour.

Robert"""}
        ],
        expected_updates=[],
        expected_events=[],  # May detect needs_user_input for tour offer
        expected_response_type="missing_fields",
        expected_notifications=[]
    ),

    TestScenario(
        name="edge_conflicting_info",
        description="Broker provides conflicting information",
        category=TestCategory.EDGE_CASES,
        property_address="699 Industrial Park Dr",
        messages=[
            {"direction": "outbound", "content": "Hi Jeff, what's the SF for 699 Industrial Park Dr?"},
            {"direction": "inbound", "content": """Hi Jill,

The space is 10,000 SF. Actually wait, let me check... it's 8,500 SF, sorry about that.

Jeff"""}
        ],
        expected_updates=[
            {"column": "Total SF", "value": "8500"},  # Should use the corrected value
        ],
        expected_events=[],
        expected_response_type="missing_fields",
        expected_notifications=[
            ExpectedNotification(kind="sheet_update"),
        ]
    ),

    TestScenario(
        name="edge_already_complete",
        description="Row already has all required fields - just acknowledging",
        category=TestCategory.EDGE_CASES,
        property_address="1 Randolph Ct",
        initial_row_data=["1 Randolph Ct", "Evans", "", "Atkins", "Scott", "scott@test.com", "15000", "8.50", "2.25", "", "2", "4", "24", "400A", "", "", "", ""],
        messages=[
            {"direction": "outbound", "content": "Thanks for confirming the details on 1 Randolph Ct!"},
            {"direction": "inbound", "content": """You're welcome! Reach out anytime if you need more info.

Scott"""}
        ],
        expected_updates=[],
        expected_events=["close_conversation"],
        expected_response_type="closing",
        expected_notifications=[]
    ),
]


# ============================================================================
# TEST RUNNER
# ============================================================================

def build_conversation(scenario: TestScenario) -> List[Dict]:
    """Build conversation payload for the AI."""
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


def derive_notifications(updates: List[Dict], events: List[Dict], row_data: List[str], header: List[str]) -> List[Dict]:
    """Derive what notifications would fire."""
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
            notifications.append({"kind": "action_needed", "reason": "call_requested"})
        elif event_type == "needs_user_input":
            reason = event.get("reason", "unclear")
            notifications.append({"kind": "action_needed", "reason": f"needs_user_input:{reason}"})
        elif event_type == "property_unavailable":
            notifications.append({"kind": "property_unavailable"})
        elif event_type == "new_property":
            notifications.append({"kind": "action_needed", "reason": "new_property_pending_send"})
        elif event_type == "contact_optout":
            reason = event.get("reason", "not_interested")
            notifications.append({"kind": "action_needed", "reason": f"contact_optout:{reason}"})
        elif event_type == "wrong_contact":
            reason = event.get("reason", "wrong_person")
            notifications.append({"kind": "action_needed", "reason": f"wrong_contact:{reason}"})
        elif event_type == "property_issue":
            notifications.append({"kind": "action_needed", "reason": "property_issue"})

    # Check if all required fields complete
    idx_map = {h.lower(): i for i, h in enumerate(header) if h}
    current_values = {h.lower(): row_data[i] if i < len(row_data) else "" for h, i in idx_map.items()}

    for update in updates:
        col = update.get("column", "").lower()
        if col:
            current_values[col] = update.get("value", "")

    required = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]
    all_complete = all(current_values.get(f, "").strip() for f in required)

    if all_complete and updates:
        notifications.append({"kind": "row_completed"})

    return notifications


def run_scenario(scenario: TestScenario, verbose: bool = True) -> TestResult:
    """Run a single test scenario."""
    result = TestResult(
        scenario_name=scenario.name,
        category=scenario.category.value
    )

    prop = PROPERTIES.get(scenario.property_address)
    if not prop:
        result.issues.append(f"Unknown property: {scenario.property_address}")
        return result

    header = scenario.header or STANDARD_HEADER
    row_data = scenario.initial_row_data or prop["data"]
    conversation = build_conversation(scenario)

    if verbose:
        print(f"\n{'='*60}")
        print(f"Test: {scenario.name} [{scenario.category.value}]")
        print(f"   {scenario.description}")
        print(f"{'='*60}")

    start = time.time()
    try:
        proposal = propose_sheet_updates(
            uid="test-user",
            client_id="test-client",
            email=prop["email"],
            sheet_id="test-sheet-id",
            header=header,
            rownum=prop["row"],
            rowvals=row_data,
            thread_id=f"test-thread-{scenario.name}",
            contact_name=prop["contact"],
            conversation=conversation,
            column_config=scenario.column_config,
            dry_run=True
        )
        elapsed = int((time.time() - start) * 1000)
        result.api_time_ms = elapsed

        if proposal is None:
            result.issues.append("propose_sheet_updates returned None")
            return result

        result.ai_updates = proposal.get("updates", [])
        result.ai_events = proposal.get("events", [])
        result.ai_response = proposal.get("response_email", "")
        result.ai_notes = proposal.get("notes", "")

        # Validate
        issues, warnings = validate_result(scenario, proposal, row_data, header)
        result.issues = issues
        result.warnings = warnings
        result.passed = len(issues) == 0

        # Derive notifications
        result.derived_notifications = derive_notifications(
            result.ai_updates, result.ai_events, row_data, header
        )

        if verbose:
            print(f"\n   Response ({elapsed}ms):")
            print(f"   Updates: {len(result.ai_updates)}")
            for u in result.ai_updates:
                print(f"      - {u.get('column')}: {u.get('value')}")
            print(f"   Events: {[e.get('type') for e in result.ai_events]}")
            print(f"   Notes: {result.ai_notes[:100]}..." if result.ai_notes else "   Notes: (none)")
            print(f"\n   {'PASS' if result.passed else 'FAIL'}")
            for i in issues:
                print(f"      - {i}")
            for w in warnings:
                print(f"      Warning: {w}")

    except Exception as e:
        result.issues.append(f"Exception: {str(e)}")
        if verbose:
            print(f"   Error: {e}")

    return result


def validate_result(scenario: TestScenario, result: Dict, row_data: List[str], header: List[str]) -> tuple:
    """Validate result against expectations."""
    issues = []
    warnings = []

    updates = result.get("updates", [])
    events = result.get("events", [])
    response = result.get("response_email", "")

    # Check updates
    actual_updates = {u["column"].lower(): u["value"] for u in updates}
    for exp in scenario.expected_updates:
        col = exp["column"]
        exp_val = str(exp["value"]).replace(",", "")

        if col.lower() not in actual_updates:
            issues.append(f"Missing update: {col}")
            continue

        act_val = str(actual_updates[col.lower()]).replace(",", "")
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

    # Check forbidden patterns in response
    if response:
        response_lower = response.lower()
        if "rent/sf /yr" in response_lower or "rent/sf/yr" in response_lower:
            issues.append("Response requests 'Rent/SF /Yr' (FORBIDDEN)")
        if "gross rent" in response_lower:
            issues.append("Response requests 'Gross Rent' (FORBIDDEN)")

    # Check AI didn't write to Gross Rent
    for u in updates:
        if u.get("column", "").lower() == "gross rent":
            issues.append("AI tried to write to 'Gross Rent' (FORBIDDEN)")

    # Check escalation scenarios
    if scenario.expected_response_type == "escalate":
        if "needs_user_input" not in actual_event_types:
            issues.append("Expected 'needs_user_input' event for escalation")
        if response and response.strip():
            issues.append("Response should be empty when escalating")

    return issues, warnings


def run_all(category: str = None, verbose: bool = True) -> List[TestResult]:
    """Run all test scenarios."""
    scenarios = SCENARIOS
    if category:
        cat_enum = TestCategory(category)
        scenarios = [s for s in SCENARIOS if s.category == cat_enum]

    print("\n" + "="*70)
    print("E2E TEST SUITE")
    print("="*70)
    print(f"Running {len(scenarios)} scenarios...")

    results = []
    for scenario in scenarios:
        result = run_scenario(scenario, verbose=verbose)
        results.append(result)
        time.sleep(0.3)

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed}")
    print(f"Pass Rate: {passed/len(results)*100:.1f}%")

    # By category
    categories = {}
    for r in results:
        if r.category not in categories:
            categories[r.category] = {"passed": 0, "failed": 0}
        if r.passed:
            categories[r.category]["passed"] += 1
        else:
            categories[r.category]["failed"] += 1

    print("\nBy Category:")
    for cat, counts in categories.items():
        total = counts["passed"] + counts["failed"]
        print(f"   {cat}: {counts['passed']}/{total} passed")

    if failed > 0:
        print("\nFailed tests:")
        for r in results:
            if not r.passed:
                print(f"\n   {r.scenario_name}:")
                for i in r.issues:
                    print(f"      - {i}")

    return results


def test_column_detection():
    """Test the column detection system."""
    print("\n" + "="*70)
    print("COLUMN DETECTION TEST")
    print("="*70)

    # Test with standard headers
    print("\n1. Standard headers:")
    result = detect_column_mapping(STANDARD_HEADER, use_ai=False)
    print(f"   Mapped: {len(result['mappings'])} fields")
    for canonical, actual in result['mappings'].items():
        print(f"      {canonical} -> {actual}")

    # Test with renamed headers
    print("\n2. Renamed headers (using AI):")
    result = detect_column_mapping(ALT_HEADER_RENAMED, use_ai=True)
    print(f"   Mapped: {len(result['mappings'])} fields")
    for canonical, actual in result['mappings'].items():
        conf = result['confidence'].get(canonical, 0)
        print(f"      {canonical} -> {actual} (conf: {conf:.2f})")

    print(f"   Unmapped: {result['unmapped']}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="E2E Test Suite")
    parser.add_argument("-s", "--scenario", help="Run specific scenario")
    parser.add_argument("-c", "--category", help="Run scenarios in category")
    parser.add_argument("-l", "--list", action="store_true", help="List scenarios")
    parser.add_argument("-q", "--quiet", action="store_true", help="Less output")
    parser.add_argument("--detect", action="store_true", help="Test column detection")

    args = parser.parse_args()

    if args.list:
        print("\nAvailable scenarios:")
        for cat in TestCategory:
            print(f"\n  [{cat.value}]")
            for s in SCENARIOS:
                if s.category == cat:
                    print(f"    - {s.name}: {s.description}")
    elif args.detect:
        test_column_detection()
    elif args.scenario:
        scenario = next((s for s in SCENARIOS if s.name == args.scenario), None)
        if scenario:
            run_scenario(scenario, verbose=not args.quiet)
        else:
            print(f"Scenario '{args.scenario}' not found")
    else:
        run_all(category=args.category, verbose=not args.quiet)
