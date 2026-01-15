#!/usr/bin/env python3
"""
Standalone Test Runner
======================
Tests the AI extraction logic directly with OpenAI API.
Does not require Firebase or Google Sheets - only OpenAI.
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Any
from dataclasses import dataclass, field

# Check for OpenAI API key
if not os.getenv("OPENAI_API_KEY"):
    print("❌ OPENAI_API_KEY environment variable not set")
    print("Please set it: export OPENAI_API_KEY='your-key'")
    sys.exit(1)

from openai import OpenAI

# Initialize OpenAI client
client = OpenAI()

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
REQUIRED_FIELDS = ["Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"]

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
    }
}


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
        expected_response_type="closing"
        # Expected notes: should capture "available immediately", "built 2019", "fenced yard", "15 trailer spots", "landlord motivated", "3-5 yr flexible", "off I-20"
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
        expected_response_type="missing_fields"
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
        expected_response_type="unavailable"
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
        expected_response_type="unavailable_with_alternative"
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
        expected_response_type="call_with_phone"
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
        expected_response_type="ask_for_phone"
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
        expected_response_type="missing_fields"  # Still missing Power
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
        expected_response_type="missing_fields"
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
        expected_response_type="missing_fields"
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
        expected_response_type="closing"
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
        expected_response_type="escalate"
        # AI should NOT respond - user needs to provide client requirements
    ),

    TestScenario(
        name="scheduling_request",
        description="Broker requests tour scheduling - AI cannot commit to times",
        property_address="1 Randolph Ct",
        messages=[
            {"direction": "outbound", "content": "Hi Scott, interested in touring 1 Randolph Ct."},
            {"direction": "inbound", "content": """Hi Jill,

Great! Can you come by Tuesday at 2pm for a tour? Or would Wednesday morning work better?

Scott"""}
        ],
        expected_updates=[],
        expected_events=["needs_user_input"],
        expected_response_type="escalate"
        # AI should NOT respond - user needs to confirm schedule
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
        expected_response_type="escalate"
        # AI should NOT respond - user needs to handle negotiation
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
        expected_response_type="escalate"
        # AI should NOT reveal client identity
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
        expected_response_type="escalate"
        # AI should NOT respond to contract/legal requests
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
        expected_response_type="escalate"
        # Should extract data BUT still escalate due to unanswerable questions
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
        expected_response_type="escalate"
        # AI should NOT reveal budget information
    ),
]


def build_prompt(scenario: TestScenario) -> str:
    """Build the OpenAI prompt for a scenario."""
    prop = PROPERTIES.get(scenario.property_address)
    if not prop:
        raise ValueError(f"Unknown property: {scenario.property_address}")

    target_anchor = f"{scenario.property_address}, {prop['city']}"
    contact_name = prop["contact"]
    row_data = prop["data"]

    # Build conversation
    conversation = []
    for i, msg in enumerate(scenario.messages):
        conversation.append({
            "direction": msg["direction"],
            "from": prop["email"] if msg["direction"] == "inbound" else "jill@company.com",
            "to": ["jill@company.com"] if msg["direction"] == "inbound" else [prop["email"]],
            "subject": target_anchor,
            "timestamp": f"2024-01-15T{10+i}:00:00Z",
            "content": msg["content"]
        })

    # Calculate missing fields
    header_map = {h.strip().lower(): i for i, h in enumerate(HEADER)}
    missing = []
    for field in REQUIRED_FIELDS:
        idx = header_map.get(field.strip().lower())
        if idx is not None and idx < len(row_data):
            if not row_data[idx].strip():
                missing.append(field)

    prompt = f"""
You are analyzing a conversation thread to suggest updates to ONE Google Sheet row, detect key events, and generate an appropriate response email.

You are acting on behalf of "Jill Ames", a commercial real estate broker assistant. You help gather property information but CANNOT make decisions for the client.

TARGET PROPERTY: {target_anchor}
CONTACT NAME: {contact_name}

COLUMN SEMANTICS (use EXACT header names):
- "Rent/SF /Yr": Base/asking rent per square foot per YEAR.
- "Ops Ex /SF": NNN/CAM/Operating Expenses per square foot per YEAR.
- "Gross Rent": DO NOT WRITE - this is a formula column that auto-calculates. NEVER include in updates.
- "Total SF": Total square footage.
- "Drive Ins": Number of drive-in doors.
- "Docks": Number of dock doors.
- "Ceiling Ht": Ceiling height (just the number).
- "Power": Electrical specifications.

FORMATTING: Plain decimals, no "$" or "SF" symbols. Just numbers like "15000", "8.50", "24".

EVENTS (analyze LAST HUMAN message only):
- "property_unavailable": Property explicitly stated as unavailable/leased/off-market.
- "new_property": Different property suggested (different address/URL).
- "call_requested": Explicit request for phone call (use this, NOT needs_user_input, for call requests).
- "close_conversation": Conversation appears complete.
- "needs_user_input": CRITICAL - Use when AI CANNOT or SHOULD NOT respond. Triggers when (but NOT for call requests - use call_requested for those):
  * Broker asks about client requirements (size needed, budget, timeline, move-in date)
  * Scheduling requests (tour times, in-person meeting requests - NOT phone calls)
  * Negotiation attempts (counteroffers, price discussions, lease term negotiations)
  * Questions about client identity ("who is your client?", "what company?")
  * Legal/contract questions ("send LOI", "when can you sign?", "what terms?")
  * Confusing or unclear messages
  Include "reason" field: client_question | scheduling | negotiation | confidential | legal_contract | unclear
  Include "question" field: the specific question/request needing user attention

NOTES (capture useful details not in columns):
ALWAYS capture when mentioned: availability timing, lease terms, zoning, special features (fenced yard, rail spur, sprinklered), parking, landlord notes (owner motivated, firm on price), building age, location context (near I-20), divisibility, HVAC, office space details.
FORMAT: Terse fragments separated by " • ". Example: "available immediately • 3-5 yr preferred • fenced yard"
IMPORTANT: Don't leave notes empty if useful info exists in the conversation.

RESPONSE EMAIL RULES:
- Start with "Hi," or similar greeting
- NO closing like "Best," - footer adds it automatically
- NEVER request "Rent/SF /Yr" or "Gross Rent"
- End with simple "Thanks" not "Looking forward to..."
- SET response_email TO NULL when needs_user_input event is emitted - let the user respond instead
- You can still extract data updates even when escalating (e.g., broker provides some info but also asks questions)

SHEET HEADER: {json.dumps(HEADER)}

CURRENT ROW: {json.dumps(row_data)}

MISSING FIELDS: {json.dumps(missing)}

CONVERSATION:
{json.dumps(conversation, indent=2)}

OUTPUT valid JSON only:
{{
  "updates": [{{"column": "...", "value": "...", "confidence": 0.9, "reason": "..."}}],
  "events": [{{"type": "...", "reason": "<for needs_user_input>", "question": "<specific question>"}}],
  "response_email": "<null if needs_user_input event>",
  "notes": "<capture useful non-column info: timing, terms, features, etc. Use ' • ' separator>"
}}
"""
    return prompt


def call_openai(prompt: str) -> tuple:
    """Call OpenAI API. Returns (response_dict, time_ms)."""
    start = time.time()
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        elapsed = int((time.time() - start) * 1000)
        raw = response.choices[0].message.content.strip()

        # Clean up JSON if wrapped in code block
        if raw.startswith("```"):
            lines = raw.split("\n")
            json_lines = []
            in_json = False
            for line in lines:
                if line.strip().startswith("```"):
                    in_json = not in_json
                    continue
                if in_json:
                    json_lines.append(line)
            raw = "\n".join(json_lines)

        return json.loads(raw), elapsed

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}", "raw": raw}, int((time.time() - start) * 1000)
    except Exception as e:
        return {"error": str(e)}, int((time.time() - start) * 1000)


def validate_result(scenario: TestScenario, result: Dict) -> tuple:
    """Validate result against expectations. Returns (passed, issues, warnings)."""
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

    # Check escalation scenarios
    if scenario.expected_response_type == "escalate":
        # When escalating, AI should emit needs_user_input and NOT generate a response
        if "needs_user_input" not in actual_event_types:
            issues.append("Expected 'needs_user_input' event for escalation scenario")

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

    # Check that response_email is present for non-escalation scenarios (except call_requested with phone)
    elif scenario.expected_response_type not in ["escalate", "call_with_phone"]:
        if "needs_user_input" in actual_event_types:
            # AI escalated when it shouldn't have
            warnings.append("AI escalated to user when it could have responded automatically")

    passed = len(issues) == 0
    return passed, issues, warnings


def run_test(scenario: TestScenario, verbose: bool = True) -> TestResult:
    """Run a single test scenario."""
    result = TestResult(scenario_name=scenario.name)

    if verbose:
        print(f"\n{'='*60}")
        print(f"🧪 {scenario.name}")
        print(f"   {scenario.description}")
        print(f"   Property: {scenario.property_address}")
        print(f"{'='*60}")

        print("\n   Conversation:")
        for msg in scenario.messages:
            arrow = "   →" if msg["direction"] == "outbound" else "   ←"
            preview = msg["content"][:60].replace('\n', ' ')
            print(f"   {arrow} {preview}...")

    # Build and send prompt
    prompt = build_prompt(scenario)

    if verbose:
        print("\n   Calling OpenAI...")

    ai_result, elapsed = call_openai(prompt)
    result.api_time_ms = elapsed

    if "error" in ai_result:
        result.issues.append(f"API Error: {ai_result['error']}")
        if verbose:
            print(f"   ❌ Error: {ai_result['error']}")
        return result

    result.ai_updates = ai_result.get("updates", [])
    result.ai_events = ai_result.get("events", [])
    result.ai_response = ai_result.get("response_email", "")
    result.ai_notes = ai_result.get("notes", "")

    if verbose:
        print(f"\n   Response ({elapsed}ms):")
        print(f"   Updates: {len(result.ai_updates)}")
        for u in result.ai_updates:
            print(f"      • {u.get('column')}: {u.get('value')} (conf: {u.get('confidence', 'N/A')})")

        print(f"   Events: {[e.get('type') for e in result.ai_events]}")

        # Show details for needs_user_input events
        for e in result.ai_events:
            if e.get("type") == "needs_user_input":
                print(f"   ⚠️ Escalation: reason={e.get('reason', 'N/A')}")
                if e.get("question"):
                    print(f"      Question: {e.get('question')[:80]}...")

        if result.ai_response:
            preview = result.ai_response[:80].replace('\n', ' ')
            print(f"   Response email: {preview}...")
        else:
            print(f"   Response email: (none - escalated to user)")

        if result.ai_notes:
            print(f"   Notes: {result.ai_notes}")

    # Validate
    passed, issues, warnings = validate_result(scenario, ai_result)
    result.passed = passed
    result.issues = issues
    result.warnings = warnings

    if verbose:
        print(f"\n   {'✅ PASS' if passed else '❌ FAIL'}")
        for i in issues:
            print(f"      • {i}")
        for w in warnings:
            print(f"      ⚠️ {w}")

    return result


def run_all(verbose: bool = True) -> List[TestResult]:
    """Run all test scenarios."""
    print("\n" + "="*70)
    print("🚀 EMAIL AUTOMATION AI TEST SUITE")
    print("="*70)
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
    print("📊 SUMMARY")
    print("="*70)
    print(f"Total: {len(results)} | ✅ Passed: {passed} | ❌ Failed: {failed}")
    print(f"Pass Rate: {passed/len(results)*100:.1f}%")

    avg_time = sum(r.api_time_ms for r in results) / len(results)
    print(f"Avg API Time: {avg_time:.0f}ms")

    if failed > 0:
        print("\n❌ Failed tests:")
        for r in results:
            if not r.passed:
                print(f"\n   {r.scenario_name}:")
                for i in r.issues:
                    print(f"      • {i}")

    return results


def save_report(results: List[TestResult], filename: str = "test_results.json"):
    """Save test results to JSON file."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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

    print(f"\n📄 Report saved to: {filename}")


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
            print(f"  • {s.name}: {s.description}")
    elif args.scenario:
        scenario = next((s for s in SCENARIOS if s.name == args.scenario), None)
        if scenario:
            result = run_test(scenario, verbose=not args.quiet)
        else:
            print(f"❌ Scenario '{args.scenario}' not found")
    else:
        results = run_all(verbose=not args.quiet)
        if args.report:
            save_report(results, args.report)
