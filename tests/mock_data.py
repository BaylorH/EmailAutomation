"""
Mock data for testing the email automation system.
Based on actual sheet structure from M.S. Augusta GA client.
"""

# Actual header structure from the real sheet (Row 2)
REAL_HEADER = [
    "Property Address",
    "City",
    "Property Name",
    "Leasing Company",
    "Leasing Contact",
    "Email",
    "Total SF",
    "Rent/SF /Yr",
    "Ops Ex /SF",
    "Gross Rent",
    "Drive Ins",
    "Docks",
    "Ceiling Ht",
    "Power",
    "Listing Brokers Comments ",  # Note: trailing space in real sheet
    "Flyer / Link",
    "Floorplan",
    "Jill and Clients comments"
]

# Sample properties from the real sheet (starting Row 3)
SAMPLE_PROPERTIES = [
    # Row 3: 699 Industrial Park Dr - has email, no data
    {
        "row": 3,
        "data": ["699 Industrial Park Dr", "Evans", "", "", "Jeff and Connie Wilson, CCIM,", "testing@gmail.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    # Row 4: 135 Trade Center Court - has email, no data
    {
        "row": 4,
        "data": ["135 Trade Center Court", "Augusta", "", "", "Luke Coffey", "testing2@gmail.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    # Row 5: 2058 Gordon Hwy - no email
    {
        "row": 5,
        "data": ["2058 Gordon Hwy", "Augusta", "Battery Clinic", "Meybohm Commercial Properties", "Jonathan Aceves", "", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    # Row 6: 1 Kuhlke Dr - no email
    {
        "row": 6,
        "data": ["1 Kuhlke Dr", "Augusta", "", "", "Robert McCrary", "", "", "", "", "", "", "", "", "", "", "", "", ""]
    },
    # Row 7: 1 Randolph Ct - test email
    {
        "row": 7,
        "data": ["1 Randolph Ct", "Evans", "", "Atkins Commercial Properties", "Scott A. Atkins CCIM, SIOR", "bp21harrison@gmail.com", "", "", "", "", "", "", "", "", "", "", "", ""]
    }
]

def create_mock_sheet():
    """Create a mock sheet structure matching real format."""
    return {
        "client_name": "M.S. Augusta GA - Dhaval and Kashyap",
        "header": REAL_HEADER.copy(),
        "rows": {prop["row"]: prop["data"].copy() for prop in SAMPLE_PROPERTIES}
    }

def get_row_by_email(sheet_data: dict, email: str) -> tuple:
    """Find row by email address."""
    email_lower = email.lower().strip()
    header = sheet_data["header"]

    # Find email column index
    email_idx = None
    for i, h in enumerate(header):
        if h.lower().strip() in ["email", "email address"]:
            email_idx = i
            break

    if email_idx is None:
        return None, None

    for row_num, row_data in sheet_data["rows"].items():
        if len(row_data) > email_idx:
            if row_data[email_idx].lower().strip() == email_lower:
                # Pad row to header length
                padded = row_data + [""] * (len(header) - len(row_data))
                return row_num, padded

    return None, None

def get_header_index_map(header: list) -> dict:
    """Create normalized header -> index mapping (1-based)."""
    return {(h or "").strip().lower(): i for i, h in enumerate(header, start=1)}


# ============================================================================
# SIMULATED EMAIL CONVERSATIONS - Various Scenarios
# ============================================================================

class ConversationScenario:
    """Represents a test scenario with conversation history and expected outcomes."""

    def __init__(self, name: str, description: str, email: str, contact_name: str,
                 property_address: str, city: str, messages: list,
                 expected_updates: list, expected_events: list,
                 expected_response_type: str, initial_row_data: list = None):
        self.name = name
        self.description = description
        self.email = email
        self.contact_name = contact_name
        self.property_address = property_address
        self.city = city
        self.messages = messages  # List of {direction, content, timestamp}
        self.expected_updates = expected_updates  # List of {column, value}
        self.expected_events = expected_events  # List of {type, ...}
        self.expected_response_type = expected_response_type  # "missing_fields", "closing", "unavailable", etc.
        self.initial_row_data = initial_row_data  # Optional custom initial row state


# ============================================================================
# TEST SCENARIOS
# ============================================================================

SCENARIOS = [
    # --------------------------------------------------
    # SCENARIO 1: Basic property info with all data
    # --------------------------------------------------
    ConversationScenario(
        name="complete_info_first_reply",
        description="Broker provides all required information in first reply",
        email="bp21harrison@gmail.com",
        contact_name="Scott Atkins",
        property_address="1 Randolph Ct",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": """Hi Scott,

I'm reaching out about the property at 1 Randolph Ct in Evans.

Could you please provide:
- Total SF
- Rent/SF /Yr
- Ops Ex /SF
- Drive Ins
- Docks
- Ceiling Ht
- Power specifications

Thanks!""",
                "timestamp": "2024-01-15T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

Happy to help! Here are the details for 1 Randolph Ct:

- Total SF: 15,000
- Asking rent: $8.50/SF/yr NNN
- NNN/CAM: $2.25/SF/yr
- 2 drive-in doors
- 4 dock doors
- Clear height: 24 feet
- Power: 400 amps, 3-phase

Let me know if you need anything else!

Best,
Scott""",
                "timestamp": "2024-01-15T14:30:00Z"
            }
        ],
        expected_updates=[
            {"column": "Total SF", "value": "15000"},
            {"column": "Rent/SF /Yr", "value": "8.50"},
            {"column": "Ops Ex /SF", "value": "2.25"},
            {"column": "Gross Rent", "value": "10.75"},
            {"column": "Drive Ins", "value": "2"},
            {"column": "Docks", "value": "4"},
            {"column": "Ceiling Ht", "value": "24"},
            {"column": "Power", "value": "400 amps, 3-phase"},
        ],
        expected_events=[],
        expected_response_type="closing"
    ),

    # --------------------------------------------------
    # SCENARIO 2: Partial info - missing fields
    # --------------------------------------------------
    ConversationScenario(
        name="partial_info_needs_followup",
        description="Broker provides only some fields, needs follow-up",
        email="testing@gmail.com",
        contact_name="Jeff Wilson",
        property_address="699 Industrial Park Dr",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": """Hi Jeff,

I'm interested in 699 Industrial Park Dr in Evans. Could you provide details?

Thanks!""",
                "timestamp": "2024-01-15T09:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi,

The space is 8,500 SF with asking rent of $6.00/SF NNN.

Jeff""",
                "timestamp": "2024-01-15T11:00:00Z"
            }
        ],
        expected_updates=[
            {"column": "Total SF", "value": "8500"},
            {"column": "Rent/SF /Yr", "value": "6.00"},
        ],
        expected_events=[],
        expected_response_type="missing_fields"  # Should ask for: Ops Ex, Gross Rent, Drive Ins, Docks, Ceiling Ht, Power
    ),

    # --------------------------------------------------
    # SCENARIO 3: Property unavailable
    # --------------------------------------------------
    ConversationScenario(
        name="property_unavailable",
        description="Broker says property is no longer available",
        email="testing2@gmail.com",
        contact_name="Luke Coffey",
        property_address="135 Trade Center Court",
        city="Augusta",
        messages=[
            {
                "direction": "outbound",
                "content": """Hi Luke,

Following up on 135 Trade Center Court. Do you have availability details?

Thanks!""",
                "timestamp": "2024-01-14T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

Unfortunately that property is no longer available - it was leased last week.

Luke""",
                "timestamp": "2024-01-15T08:00:00Z"
            }
        ],
        expected_updates=[],
        expected_events=[
            {"type": "property_unavailable"}
        ],
        expected_response_type="unavailable_ask_alternatives"
    ),

    # --------------------------------------------------
    # SCENARIO 4: Property unavailable + new property suggested
    # --------------------------------------------------
    ConversationScenario(
        name="unavailable_with_alternative",
        description="Property unavailable but broker suggests alternative",
        email="bp21harrison@gmail.com",
        contact_name="Scott Atkins",
        property_address="1 Randolph Ct",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": """Hi Scott,

Is 1 Randolph Ct still available?

Thanks!""",
                "timestamp": "2024-01-15T09:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

Sorry, 1 Randolph Ct is no longer available - we just signed a lease yesterday.

However, I do have another property that might work for you:
456 Commerce Blvd in Martinez - similar size at around 12,000 SF.

Here's the listing: https://example.com/456-commerce

Let me know if you'd like details!

Scott""",
                "timestamp": "2024-01-15T14:00:00Z"
            }
        ],
        expected_updates=[],
        expected_events=[
            {"type": "property_unavailable"},
            {"type": "new_property", "address": "456 Commerce Blvd", "city": "Martinez"}
        ],
        expected_response_type="unavailable_with_new_property"
    ),

    # --------------------------------------------------
    # SCENARIO 5: Call requested with phone number
    # --------------------------------------------------
    ConversationScenario(
        name="call_requested_with_phone",
        description="Broker requests a call and provides phone number",
        email="testing@gmail.com",
        contact_name="Jeff Wilson",
        property_address="699 Industrial Park Dr",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Jeff, following up on 699 Industrial Park Dr.",
                "timestamp": "2024-01-15T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

I'd prefer to discuss this over the phone - there are some details that would be easier to explain.

Can you give me a call at (706) 555-1234?

Thanks,
Jeff""",
                "timestamp": "2024-01-15T15:00:00Z"
            }
        ],
        expected_updates=[],
        expected_events=[
            {"type": "call_requested", "phone": "(706) 555-1234"}
        ],
        expected_response_type="skip_response"  # Phone provided = notification only
    ),

    # --------------------------------------------------
    # SCENARIO 6: Call requested without phone number
    # --------------------------------------------------
    ConversationScenario(
        name="call_requested_no_phone",
        description="Broker requests a call but doesn't provide number",
        email="testing2@gmail.com",
        contact_name="Luke Coffey",
        property_address="135 Trade Center Court",
        city="Augusta",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Luke, checking on 135 Trade Center Court availability.",
                "timestamp": "2024-01-15T09:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi,

Can we schedule a call to discuss? I have several options that might work.

Luke""",
                "timestamp": "2024-01-15T13:00:00Z"
            }
        ],
        expected_updates=[],
        expected_events=[
            {"type": "call_requested"}
        ],
        expected_response_type="ask_for_phone"
    ),

    # --------------------------------------------------
    # SCENARIO 7: Multi-turn conversation with incremental updates
    # --------------------------------------------------
    ConversationScenario(
        name="multi_turn_incremental",
        description="Multiple exchanges gradually filling in data",
        email="bp21harrison@gmail.com",
        contact_name="Scott Atkins",
        property_address="1 Randolph Ct",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Scott, interested in 1 Randolph Ct. What's the SF and rent?",
                "timestamp": "2024-01-14T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": "Hi Jill, it's 20,000 SF at $7.50/SF NNN. Scott",
                "timestamp": "2024-01-14T14:00:00Z"
            },
            {
                "direction": "outbound",
                "content": "Thanks! What are the NNN expenses and dock/door count?",
                "timestamp": "2024-01-14T15:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """NNN is $1.85/SF.

We have 3 dock doors and 1 drive-in. Ceiling is 20' clear.

Scott""",
                "timestamp": "2024-01-15T09:00:00Z"
            }
        ],
        expected_updates=[
            {"column": "Total SF", "value": "20000"},
            {"column": "Rent/SF /Yr", "value": "7.50"},
            {"column": "Ops Ex /SF", "value": "1.85"},
            {"column": "Gross Rent", "value": "9.35"},
            {"column": "Docks", "value": "3"},
            {"column": "Drive Ins", "value": "1"},
            {"column": "Ceiling Ht", "value": "20"},
        ],
        expected_events=[],
        expected_response_type="missing_fields"  # Still missing Power
    ),

    # --------------------------------------------------
    # SCENARIO 8: PDF attachment provides data
    # --------------------------------------------------
    ConversationScenario(
        name="pdf_attachment_data",
        description="Broker sends PDF flyer with property details",
        email="testing@gmail.com",
        contact_name="Jeff Wilson",
        property_address="699 Industrial Park Dr",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Jeff, do you have a flyer for 699 Industrial Park Dr?",
                "timestamp": "2024-01-15T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

Attached is the property flyer with all the details.

Let me know if you have questions!

Jeff""",
                "timestamp": "2024-01-15T14:00:00Z",
                "attachment_content": """
699 INDUSTRIAL PARK DR - EVANS, GA

PROPERTY SPECIFICATIONS:
- Total Size: 12,500 SF
- Asking Rent: $5.75/SF/YR (NNN)
- Operating Expenses: $1.50/SF/YR
- Clear Height: 18 feet
- Loading: 2 dock-high doors, 1 grade-level door
- Power: 200 amp, single phase
- Zoning: Industrial

Contact: Jeff Wilson, CCIM
Phone: (706) 555-0000
"""
            }
        ],
        expected_updates=[
            {"column": "Total SF", "value": "12500"},
            {"column": "Rent/SF /Yr", "value": "5.75"},
            {"column": "Ops Ex /SF", "value": "1.50"},
            {"column": "Gross Rent", "value": "7.25"},
            {"column": "Ceiling Ht", "value": "18"},
            {"column": "Docks", "value": "2"},
            {"column": "Drive Ins", "value": "1"},
            {"column": "Power", "value": "200 amp, single phase"},
        ],
        expected_events=[],
        expected_response_type="closing"
    ),

    # --------------------------------------------------
    # SCENARIO 9: URL with property details
    # --------------------------------------------------
    ConversationScenario(
        name="url_with_property_data",
        description="Broker sends link to property listing",
        email="testing2@gmail.com",
        contact_name="Luke Coffey",
        property_address="135 Trade Center Court",
        city="Augusta",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Luke, interested in 135 Trade Center Court.",
                "timestamp": "2024-01-15T09:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

Here's the listing with all the details:
https://example.com/listings/135-trade-center

Luke""",
                "timestamp": "2024-01-15T12:00:00Z",
                "url_content": {
                    "url": "https://example.com/listings/135-trade-center",
                    "text": """
135 Trade Center Court - Augusta, GA

Available Industrial Space

Size: 25,000 SF
Rent: $6.25 per SF per year (NNN)
NNN Expenses: $2.00/SF/YR
Features:
- 22 foot clear ceiling height
- 4 dock doors
- 2 drive-in doors
- 400 amp, 3-phase power
"""
                }
            }
        ],
        expected_updates=[
            {"column": "Total SF", "value": "25000"},
            {"column": "Rent/SF /Yr", "value": "6.25"},
            {"column": "Ops Ex /SF", "value": "2.00"},
            {"column": "Gross Rent", "value": "8.25"},
            {"column": "Ceiling Ht", "value": "22"},
            {"column": "Docks", "value": "4"},
            {"column": "Drive Ins", "value": "2"},
            {"column": "Power", "value": "400 amp, 3-phase"},
        ],
        expected_events=[],
        expected_response_type="closing"
    ),

    # --------------------------------------------------
    # SCENARIO 10: Conflicting information (PDF vs email)
    # --------------------------------------------------
    ConversationScenario(
        name="conflicting_info_pdf_wins",
        description="Email and PDF have different numbers - PDF should win",
        email="bp21harrison@gmail.com",
        contact_name="Scott Atkins",
        property_address="1 Randolph Ct",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Scott, need details on 1 Randolph Ct.",
                "timestamp": "2024-01-15T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

I think it's around 10,000 SF at maybe $8/SF. See attached flyer for exact numbers.

Scott""",
                "timestamp": "2024-01-15T15:00:00Z",
                "attachment_content": """
1 RANDOLPH CT - EVANS, GA

ACCURATE SPECIFICATIONS:
- Total Size: 11,500 SF (not 10,000)
- Asking Rent: $7.75/SF/YR NNN
- NNN: $1.90/SF/YR
- Clear Height: 21'
- Docks: 3
- Drive-Ins: 2
- Power: 300A 3-phase
"""
            }
        ],
        expected_updates=[
            {"column": "Total SF", "value": "11500"},  # PDF value, not email
            {"column": "Rent/SF /Yr", "value": "7.75"},  # PDF value, not email
            {"column": "Ops Ex /SF", "value": "1.90"},
            {"column": "Gross Rent", "value": "9.65"},
            {"column": "Ceiling Ht", "value": "21"},
            {"column": "Docks", "value": "3"},
            {"column": "Drive Ins", "value": "2"},
            {"column": "Power", "value": "300A 3-phase"},
        ],
        expected_events=[],
        expected_response_type="closing"
    ),

    # --------------------------------------------------
    # SCENARIO 11: Auto-reply (should be skipped)
    # --------------------------------------------------
    ConversationScenario(
        name="auto_reply_skip",
        description="Out of office auto-reply should be ignored",
        email="testing@gmail.com",
        contact_name="Jeff Wilson",
        property_address="699 Industrial Park Dr",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Jeff, following up on 699 Industrial Park Dr.",
                "timestamp": "2024-01-15T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Out of Office: I am currently out of the office until January 20th with limited access to email. For urgent matters, please contact our office at (706) 555-0000.

Thank you,
Jeff Wilson""",
                "timestamp": "2024-01-15T10:01:00Z",
                "is_auto_reply": True
            }
        ],
        expected_updates=[],
        expected_events=[],
        expected_response_type="skip"  # Should not process or respond
    ),

    # --------------------------------------------------
    # SCENARIO 12: New property only (no unavailable)
    # --------------------------------------------------
    ConversationScenario(
        name="new_property_suggestion",
        description="Broker proactively suggests additional property",
        email="testing2@gmail.com",
        contact_name="Luke Coffey",
        property_address="135 Trade Center Court",
        city="Augusta",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Luke, any updates on 135 Trade Center Court?",
                "timestamp": "2024-01-15T09:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi Jill,

135 Trade Center is still available at 25,000 SF.

Also, we just got a new listing you might like:
200 Warehouse Way in North Augusta - 30,000 SF
https://example.com/200-warehouse-way

Both are good options for your client's needs.

Luke""",
                "timestamp": "2024-01-15T14:00:00Z"
            }
        ],
        expected_updates=[
            {"column": "Total SF", "value": "25000"},
        ],
        expected_events=[
            {"type": "new_property", "address": "200 Warehouse Way", "city": "North Augusta"}
        ],
        expected_response_type="missing_fields"  # Original property still needs more data
    ),

    # --------------------------------------------------
    # SCENARIO 13: Human override scenario
    # --------------------------------------------------
    ConversationScenario(
        name="human_override_respected",
        description="AI previously wrote value, human changed it - should not overwrite",
        email="bp21harrison@gmail.com",
        contact_name="Scott Atkins",
        property_address="1 Randolph Ct",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Scott, confirming details for 1 Randolph Ct.",
                "timestamp": "2024-01-16T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """The space is 18,000 SF.

Scott""",
                "timestamp": "2024-01-16T14:00:00Z"
            }
        ],
        expected_updates=[
            # Total SF should be skipped if human already modified it
            {"column": "Total SF", "value": "18000", "should_skip_if_human_override": True},
        ],
        expected_events=[],
        expected_response_type="missing_fields",
        initial_row_data=["1 Randolph Ct", "Evans", "", "Atkins Commercial Properties", "Scott A. Atkins CCIM, SIOR", "bp21harrison@gmail.com", "17500", "", "", "", "", "", "", "", "", "", "", ""]  # Human set SF to 17500
    ),

    # --------------------------------------------------
    # SCENARIO 14: Ambiguous response needs clarification
    # --------------------------------------------------
    ConversationScenario(
        name="vague_response",
        description="Broker gives vague response without clear data",
        email="testing@gmail.com",
        contact_name="Jeff Wilson",
        property_address="699 Industrial Park Dr",
        city="Evans",
        messages=[
            {
                "direction": "outbound",
                "content": "Hi Jeff, what's the rent and SF for 699 Industrial Park Dr?",
                "timestamp": "2024-01-15T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """Hi,

The rent is competitive for the area. It's a nice sized building with good loading.

Let me know if you want to tour.

Jeff""",
                "timestamp": "2024-01-15T13:00:00Z"
            }
        ],
        expected_updates=[],  # No concrete data to extract
        expected_events=[],
        expected_response_type="missing_fields"  # Still need actual numbers
    ),

    # --------------------------------------------------
    # SCENARIO 15: Close conversation event
    # --------------------------------------------------
    ConversationScenario(
        name="close_conversation",
        description="Conversation naturally concludes",
        email="testing2@gmail.com",
        contact_name="Luke Coffey",
        property_address="135 Trade Center Court",
        city="Augusta",
        messages=[
            {
                "direction": "outbound",
                "content": "Thanks for all the info on 135 Trade Center Court!",
                "timestamp": "2024-01-16T10:00:00Z"
            },
            {
                "direction": "inbound",
                "content": """You're welcome! Let me know if you need anything else. Good luck with your search!

Luke""",
                "timestamp": "2024-01-16T11:00:00Z"
            }
        ],
        expected_updates=[],
        expected_events=[
            {"type": "close_conversation"}
        ],
        expected_response_type="closing"
    ),
]


def get_scenario_by_name(name: str) -> ConversationScenario:
    """Get a specific scenario by name."""
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    return None


def get_all_scenarios() -> list:
    """Get all test scenarios."""
    return SCENARIOS
