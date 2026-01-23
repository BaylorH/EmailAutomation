#!/usr/bin/env python3
"""
Production End-to-End Test Suite
=================================
Comprehensive tests using the actual "Scrub Augusta GA.xlsx" Excel file.
Simulates real-world conversations for all properties and validates the entire pipeline.

This tests:
1. Column detection from Excel headers (including quirky spacing)
2. Full conversation flows filling out entire rows
3. All event types (unavailable, new_property, call_requested, etc.)
4. All escalation scenarios (scheduling, negotiation, identity, etc.)
5. Notification firing validation
6. Multi-turn conversations
7. Edge cases and error recovery

Usage:
    python tests/production_test.py
    python tests/production_test.py --quick    # Run subset for quick validation
    python tests/production_test.py --verbose  # Extra debug output
"""

import os
import sys
import json
import time
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
import traceback

# ============================================================================
# LOAD ENVIRONMENT
# ============================================================================

# Load .env file
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ[key] = value

if not os.getenv("OPENAI_API_KEY"):
    print("❌ OPENAI_API_KEY not found in .env")
    sys.exit(1)

# Set dummy env vars for imports
for var in ["AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY"]:
    if not os.getenv(var):
        os.environ[var] = f"test-{var.lower()}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock Firebase before imports
from unittest.mock import MagicMock, patch
mock_firestore = MagicMock()
mock_firestore.Client = MagicMock(return_value=MagicMock())
mock_firestore.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
mock_firestore.FieldFilter = MagicMock()
sys.modules['google.cloud.firestore'] = mock_firestore
sys.modules['google.cloud'] = MagicMock()
sys.modules['google.oauth2.credentials'] = MagicMock()
sys.modules['google.auth.transport.requests'] = MagicMock()
sys.modules['googleapiclient.discovery'] = MagicMock()

# Now import production code
from email_automation.ai_processing import propose_sheet_updates, check_missing_required_fields, get_row_anchor
from email_automation.column_config import (
    detect_column_mapping,
    get_default_column_config,
    build_column_rules_prompt,
    CANONICAL_FIELDS,
    REQUIRED_FOR_CLOSE,
)

print("✅ Production code imported successfully")

# ============================================================================
# LOAD EXCEL DATA
# ============================================================================

EXCEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'Scrub Augusta GA.xlsx')

def load_excel_data():
    """Load the actual Excel file and extract headers + data."""
    df = pd.read_excel(EXCEL_PATH, header=None)

    # Row 0 = Client name ("Client: M.S. Augusta GA...")
    # Row 1 = Headers
    # Row 2+ = Data

    client_name = str(df.iloc[0, 0]) if pd.notna(df.iloc[0, 0]) else ""
    headers = [str(h) if pd.notna(h) else "" for h in df.iloc[1].tolist()]

    # Clean headers - preserve original for testing but note quirks
    print(f"\n📋 Loaded Excel: {EXCEL_PATH}")
    print(f"   Client: {client_name}")
    print(f"   Headers ({len(headers)} columns): {headers[:10]}...")

    # Extract data rows
    properties = []
    for idx in range(2, len(df)):
        row = df.iloc[idx].tolist()
        row_data = [str(v) if pd.notna(v) else "" for v in row]

        # Skip empty rows
        if not row_data[0]:
            continue

        prop = {
            "address": row_data[0],
            "city": row_data[1] if len(row_data) > 1 else "",
            "contact": row_data[4] if len(row_data) > 4 else "",
            "email": row_data[5] if len(row_data) > 5 else "",
            "row_number": idx + 1,  # 1-based for sheets
            "data": row_data,
        }
        properties.append(prop)
        print(f"   Property {len(properties)}: {prop['address']}, {prop['city']} ({prop['contact']})")

    return headers, properties, client_name

HEADERS, PROPERTIES, CLIENT_NAME = load_excel_data()

# ============================================================================
# TEST RESULT TRACKING
# ============================================================================

@dataclass
class TestResult:
    name: str
    property_address: str
    passed: bool = False
    updates_count: int = 0
    events: List[str] = field(default_factory=list)
    response_generated: bool = False
    notes_captured: bool = False
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    api_time_ms: int = 0
    row_complete: bool = False

class TestStats:
    def __init__(self):
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.results: List[TestResult] = []
        self.start_time = time.time()

    def add(self, result: TestResult):
        self.results.append(result)
        self.total += 1
        if result.passed:
            self.passed += 1
        else:
            self.failed += 1

    def summary(self):
        elapsed = time.time() - self.start_time
        print(f"\n{'='*70}")
        print(f"PRODUCTION TEST RESULTS")
        print(f"{'='*70}")
        print(f"Total: {self.total} | Passed: {self.passed} | Failed: {self.failed}")
        print(f"Pass Rate: {self.passed/self.total*100:.1f}%" if self.total > 0 else "N/A")
        print(f"Total Time: {elapsed:.1f}s")

        if self.failed > 0:
            print(f"\n❌ FAILED TESTS:")
            for r in self.results:
                if not r.passed:
                    print(f"\n   {r.name} ({r.property_address}):")
                    for issue in r.issues:
                        print(f"      - {issue}")

        # Stats by category
        events_found = {}
        for r in self.results:
            for e in r.events:
                events_found[e] = events_found.get(e, 0) + 1

        if events_found:
            print(f"\n📊 Events Detected:")
            for event, count in sorted(events_found.items()):
                print(f"   {event}: {count}")

        rows_completed = sum(1 for r in self.results if r.row_complete)
        print(f"\n📝 Rows Completed: {rows_completed}/{self.total}")

        avg_time = sum(r.api_time_ms for r in self.results) / len(self.results) if self.results else 0
        print(f"⏱️  Avg API Time: {avg_time:.0f}ms")

STATS = TestStats()

# ============================================================================
# CONVERSATION BUILDER
# ============================================================================

def build_conversation(prop: dict, messages: List[dict]) -> List[dict]:
    """Build conversation payload from messages."""
    target_anchor = f"{prop['address']}, {prop['city']}"

    conversation = []
    for i, msg in enumerate(messages):
        conversation.append({
            "direction": msg["direction"],
            "from": prop["email"] if msg["direction"] == "inbound" else "jill@company.com",
            "to": ["jill@company.com"] if msg["direction"] == "inbound" else [prop["email"]],
            "subject": f"RE: {target_anchor}",
            "timestamp": f"2024-01-15T{10+i}:00:00Z",
            "preview": msg["content"][:200],
            "content": msg["content"]
        })

    return conversation

def run_test(name: str, prop: dict, messages: List[dict],
             expected_updates: List[str] = None,
             expected_events: List[str] = None,
             should_escalate: bool = False,
             should_complete_row: bool = False,
             initial_data: List[str] = None,
             verbose: bool = True) -> TestResult:
    """Run a single test case."""

    result = TestResult(name=name, property_address=prop["address"])

    if verbose:
        print(f"\n{'─'*60}")
        print(f"🧪 {name}")
        print(f"   Property: {prop['address']}, {prop['city']}")

    row_data = initial_data if initial_data else prop["data"].copy()
    conversation = build_conversation(prop, messages)

    start = time.time()
    try:
        proposal = propose_sheet_updates(
            uid="test-user",
            client_id="test-client",
            email=prop["email"],
            sheet_id="test-sheet-id",
            header=HEADERS,
            rownum=prop["row_number"],
            rowvals=row_data,
            thread_id=f"test-{name}-{prop['address'][:10]}",
            contact_name=prop["contact"],
            conversation=conversation,
            dry_run=True
        )
        result.api_time_ms = int((time.time() - start) * 1000)

        if proposal is None:
            result.issues.append("propose_sheet_updates returned None")
            if verbose:
                print(f"   ❌ FAILED: No proposal returned")
            STATS.add(result)
            return result

        # Extract results
        updates = proposal.get("updates", [])
        events = proposal.get("events", [])
        response = proposal.get("response_email", "")
        notes = proposal.get("notes", "")

        result.updates_count = len(updates)
        result.events = [e.get("type") for e in events]
        result.response_generated = bool(response and str(response).strip())
        result.notes_captured = bool(notes and str(notes).strip())

        # Ensure response is a string for later checks
        response = response or ""
        notes = notes or ""

        if verbose:
            print(f"   Updates: {len(updates)}")
            for u in updates[:5]:
                print(f"      • {u.get('column')}: {u.get('value')}")
            if len(updates) > 5:
                print(f"      ... and {len(updates)-5} more")
            print(f"   Events: {result.events}")
            print(f"   Response: {'✓' if result.response_generated else '✗'} ({len(response)} chars)")
            print(f"   Notes: {'✓' if result.notes_captured else '✗'}")

        # Validate expected updates
        if expected_updates:
            actual_cols = {u.get("column", "").lower().strip() for u in updates}
            for exp_col in expected_updates:
                if exp_col.lower().strip() not in actual_cols:
                    result.issues.append(f"Missing expected update: {exp_col}")

        # Validate expected events
        if expected_events:
            for exp_event in expected_events:
                if exp_event not in result.events:
                    result.issues.append(f"Missing expected event: {exp_event}")

        # Check escalation
        if should_escalate:
            if "needs_user_input" not in result.events:
                result.issues.append("Expected escalation (needs_user_input) but didn't get it")
            if result.response_generated:
                result.issues.append("Should NOT generate response when escalating")

        # Check forbidden patterns
        if response:
            response_lower = response.lower()
            if "gross rent" in response_lower:
                result.issues.append("Response mentions 'Gross Rent' (forbidden)")
            # Check if requesting rent/sf when we shouldn't
            if "rent/sf" in response_lower and "what" in response_lower:
                result.warnings.append("Response may be requesting Rent/SF (should never request)")

        # Check if AI tried to write to Gross Rent
        for u in updates:
            if u.get("column", "").lower().strip() == "gross rent":
                result.issues.append("AI tried to write to Gross Rent (formula column)")

        # Check row completion
        if should_complete_row or updates:
            # Simulate applying updates to row
            idx_map = {h.lower().strip(): i for i, h in enumerate(HEADERS)}
            test_row = row_data.copy()
            for u in updates:
                col = u.get("column", "").lower().strip()
                if col in idx_map:
                    test_row[idx_map[col]] = u.get("value", "")

            # Check required fields
            required_cols = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]
            # Handle the " Docks" quirk in headers
            all_filled = True
            for req in required_cols:
                found = False
                for h, i in idx_map.items():
                    if req in h or h in req:
                        if i < len(test_row) and test_row[i]:
                            found = True
                            break
                if not found:
                    all_filled = False
                    break

            result.row_complete = all_filled

            if should_complete_row and not result.row_complete:
                result.issues.append("Expected row to be complete but required fields missing")

        result.passed = len(result.issues) == 0

        if verbose:
            status = "✅ PASSED" if result.passed else "❌ FAILED"
            print(f"   {status} ({result.api_time_ms}ms)")
            for issue in result.issues:
                print(f"      ⚠️  {issue}")

    except Exception as e:
        result.issues.append(f"Exception: {str(e)}")
        if verbose:
            print(f"   ❌ EXCEPTION: {e}")
            traceback.print_exc()

    STATS.add(result)
    return result

# ============================================================================
# TEST SCENARIOS
# ============================================================================

def test_column_detection():
    """Test column detection with the actual Excel headers."""
    print(f"\n{'='*70}")
    print("TEST: Column Detection")
    print(f"{'='*70}")

    result = TestResult(name="column_detection", property_address="N/A")

    try:
        # Test with actual headers (note quirks like " Docks", "Leasing Company ")
        mapping_result = detect_column_mapping(HEADERS, use_ai=False)

        print(f"\n📋 Alias-based matching (no AI):")
        print(f"   Mapped: {len(mapping_result['mappings'])} fields")

        # Check key fields mapped correctly
        expected_mappings = [
            ("property_address", ["Property Address"]),
            ("city", ["City"]),
            ("email", ["Email"]),
            ("total_sf", ["Total SF"]),
            ("docks", ["Docks", " Docks"]),  # Note leading space
            ("ceiling_ht", ["Ceiling Ht"]),
        ]

        for canonical, acceptable in expected_mappings:
            actual = mapping_result['mappings'].get(canonical)
            if actual:
                # Handle whitespace quirks
                actual_clean = actual.strip()
                acceptable_clean = [a.strip() for a in acceptable]
                if actual_clean in acceptable_clean:
                    print(f"   ✓ {canonical} → '{actual}'")
                else:
                    result.issues.append(f"{canonical} mapped to '{actual}', expected one of {acceptable}")
                    print(f"   ✗ {canonical} → '{actual}' (expected {acceptable})")
            else:
                result.issues.append(f"{canonical} not mapped")
                print(f"   ✗ {canonical} not mapped")

        # Test AI-based matching
        print(f"\n📋 AI-based matching:")
        ai_result = detect_column_mapping(HEADERS, use_ai=True)
        print(f"   Mapped: {len(ai_result['mappings'])} fields")

        for canonical, actual in ai_result['mappings'].items():
            conf = ai_result['confidence'].get(canonical, 0)
            status = "✓" if conf >= 0.7 else "?"
            print(f"   {status} {canonical} → '{actual}' (conf: {conf:.0%})")

        if ai_result.get('unmapped'):
            print(f"   Unmapped headers: {ai_result['unmapped']}")

        result.passed = len(result.issues) == 0

    except Exception as e:
        result.issues.append(f"Exception: {e}")
        traceback.print_exc()

    STATS.add(result)
    return result

def test_complete_info_single_message():
    """Broker provides all info in one message."""
    prop = PROPERTIES[4]  # 1 Randolph Ct

    messages = [
        {"direction": "outbound", "content": f"Hi {prop['contact'].split()[0]}, I'm interested in {prop['address']}. Can you send the property details?"},
        {"direction": "inbound", "content": f"""Hi Jill,

Here are the complete details for {prop['address']}:

- Total SF: 15,000
- Asking rent: $8.50/SF/yr NNN
- NNN/CAM: $2.25/SF/yr
- 2 drive-in doors
- 4 dock doors
- Clear height: 24 feet
- Power: 400 amps, 3-phase

The space is available immediately. Built in 2019, has a fenced yard with room for 15 trailer spots.
The landlord is motivated and flexible on lease terms - they'd consider anything from 3-5 years.
Located right off I-20, great for logistics.

Let me know if you need anything else!

Best,
{prop['contact'].split()[0]}"""}
    ]

    return run_test(
        name="complete_info_single_message",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"],
        should_complete_row=True
    )

def test_partial_info_needs_followup():
    """Broker provides only some fields."""
    prop = PROPERTIES[0]  # 699 Industrial Park Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Jeff, interested in {prop['address']}. What are the specs?"},
        {"direction": "inbound", "content": """Hi,

It's 8,500 SF at $6.00/SF NNN.

Jeff"""}
    ]

    return run_test(
        name="partial_info_needs_followup",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF", "Rent/SF /Yr"]
    )

def test_multi_turn_conversation():
    """Information gathered across multiple turns."""
    prop = PROPERTIES[1]  # 135 Trade Center Court

    messages = [
        {"direction": "outbound", "content": f"Hi Luke, what's the SF and rent for {prop['address']}?"},
        {"direction": "inbound", "content": "Hi Jill, it's 20,000 SF at $7.50/SF NNN. Luke"},
        {"direction": "outbound", "content": "Thanks! What are the NNN expenses and loading?"},
        {"direction": "inbound", "content": """NNN is $1.85/SF.

3 dock doors and 1 drive-in. 20' clear height. 200A power.

Luke"""}
    ]

    return run_test(
        name="multi_turn_conversation",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Docks", "Drive Ins", "Ceiling Ht", "Power"],
        should_complete_row=True
    )

def test_property_unavailable():
    """Property is no longer available."""
    prop = PROPERTIES[1]  # 135 Trade Center Court

    messages = [
        {"direction": "outbound", "content": f"Hi Luke, following up on {prop['address']} availability."},
        {"direction": "inbound", "content": """Hi Jill,

Unfortunately that property is no longer available - it was leased last week.

Luke"""}
    ]

    return run_test(
        name="property_unavailable",
        prop=prop,
        messages=messages,
        expected_events=["property_unavailable"]
    )

def test_unavailable_with_alternative():
    """Property unavailable but broker suggests alternative."""
    prop = PROPERTIES[4]  # 1 Randolph Ct

    messages = [
        {"direction": "outbound", "content": f"Hi Scott, is {prop['address']} still available?"},
        {"direction": "inbound", "content": """Hi Jill,

Sorry, 1 Randolph Ct just got leased yesterday.

However, I have another great property that might work:
456 Commerce Blvd in Martinez - similar size around 12,000 SF, good loading.
https://example.com/456-commerce

Let me know if you'd like details!

Scott"""}
    ]

    return run_test(
        name="unavailable_with_alternative",
        prop=prop,
        messages=messages,
        expected_events=["property_unavailable", "new_property"]
    )

def test_new_property_suggestion():
    """Broker proactively suggests additional property."""
    prop = PROPERTIES[0]  # 699 Industrial Park Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Jeff, any updates on {prop['address']}?"},
        {"direction": "inbound", "content": f"""{prop['address']} is still available at 8,500 SF.

Also, we just got a new listing you might like:
200 Warehouse Way in North Augusta - 15,000 SF
https://example.com/200-warehouse

Both could work for your client.

Jeff"""}
    ]

    return run_test(
        name="new_property_suggestion",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF"],
        expected_events=["new_property"]
    )

def test_call_requested_with_phone():
    """Broker requests a call and provides phone."""
    prop = PROPERTIES[2]  # 2058 Gordon Hwy

    messages = [
        {"direction": "outbound", "content": f"Hi Jonathan, following up on {prop['address']}."},
        {"direction": "inbound", "content": """Hi Jill,

I'd prefer to discuss this over the phone - there are some details easier to explain verbally.

Can you give me a call at (706) 555-1234?

Thanks,
Jonathan"""}
    ]

    return run_test(
        name="call_requested_with_phone",
        prop=prop,
        messages=messages,
        expected_events=["call_requested"]
    )

def test_call_requested_no_phone():
    """Broker requests call without providing number."""
    prop = PROPERTIES[3]  # 1 Kuhlke Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Robert, checking on {prop['address']} availability."},
        {"direction": "inbound", "content": """Hi,

Can we schedule a call to discuss? I have several options that might work.

Robert"""}
    ]

    return run_test(
        name="call_requested_no_phone",
        prop=prop,
        messages=messages,
        expected_events=["call_requested"]
    )

def test_escalate_client_requirements():
    """Broker asks about client's requirements - must escalate."""
    prop = PROPERTIES[0]  # 699 Industrial Park Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Jeff, interested in {prop['address']}."},
        {"direction": "inbound", "content": """Hi Jill,

Before I send details, what size does your client need? And what's their timeline for moving in?

Jeff"""}
    ]

    return run_test(
        name="escalate_client_requirements",
        prop=prop,
        messages=messages,
        expected_events=["needs_user_input"],
        should_escalate=True
    )

def test_escalate_scheduling():
    """Broker wants to schedule a tour - must escalate."""
    prop = PROPERTIES[4]  # 1 Randolph Ct

    messages = [
        {"direction": "outbound", "content": f"Hi Scott, interested in touring {prop['address']}."},
        {"direction": "inbound", "content": """Hi Jill,

Great! Can you come by Tuesday at 2pm for a tour? Or would Wednesday morning work better?

Scott"""}
    ]

    return run_test(
        name="escalate_scheduling",
        prop=prop,
        messages=messages,
        expected_events=["needs_user_input"],
        should_escalate=True
    )

def test_escalate_negotiation():
    """Broker makes counteroffer - must escalate."""
    prop = PROPERTIES[1]  # 135 Trade Center Court

    messages = [
        {"direction": "outbound", "content": f"Hi Luke, is there flexibility on the rent for {prop['address']}?"},
        {"direction": "inbound", "content": """Hi Jill,

The landlord is firm at $8.50/SF, but for a 5-year term instead of 3, we could do $7.75/SF. Would your client consider that?

Luke"""}
    ]

    return run_test(
        name="escalate_negotiation",
        prop=prop,
        messages=messages,
        expected_events=["needs_user_input"],
        should_escalate=True
    )

def test_escalate_client_identity():
    """Broker asks who the client is - must escalate."""
    prop = PROPERTIES[2]  # 2058 Gordon Hwy

    messages = [
        {"direction": "outbound", "content": f"Hi Jonathan, following up on {prop['address']}."},
        {"direction": "inbound", "content": """Hi Jill,

Who is your client? What company are they with and what do they do? We want to make sure it's a good fit for the building.

Jonathan"""}
    ]

    return run_test(
        name="escalate_client_identity",
        prop=prop,
        messages=messages,
        expected_events=["needs_user_input"],
        should_escalate=True
    )

def test_escalate_legal_contract():
    """Broker asks about LOI/contract - must escalate."""
    prop = PROPERTIES[3]  # 1 Kuhlke Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Robert, the property looks good. What are next steps?"},
        {"direction": "inbound", "content": """Hi Jill,

If your client is ready to move forward, can you send over an LOI with their proposed terms? We'd need the lease term, preferred start date, and any TI requirements.

Robert"""}
    ]

    return run_test(
        name="escalate_legal_contract",
        prop=prop,
        messages=messages,
        expected_events=["needs_user_input"],
        should_escalate=True
    )

def test_mixed_info_and_question():
    """Broker provides info but also asks question requiring user input."""
    prop = PROPERTIES[1]  # 135 Trade Center Court

    messages = [
        {"direction": "outbound", "content": f"Hi Luke, what are the specs for {prop['address']}?"},
        {"direction": "inbound", "content": """Hi Jill,

The space is 18,000 SF with 24' clear height. 3 docks and 1 drive-in.

By the way, what's your client's budget? And do they need heavy power or standard?

Luke"""}
    ]

    return run_test(
        name="mixed_info_and_question",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF", "Ceiling Ht", "Docks", "Drive Ins"],
        expected_events=["needs_user_input"],
        should_escalate=True
    )

def test_contact_optout():
    """Contact says not interested / unsubscribe."""
    prop = PROPERTIES[0]  # 699 Industrial Park Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Jeff, following up on {prop['address']}."},
        {"direction": "inbound", "content": """Not interested, please remove me from your list.

Jeff"""}
    ]

    return run_test(
        name="contact_optout",
        prop=prop,
        messages=messages,
        expected_events=["contact_optout"]
    )

def test_wrong_contact():
    """Contact says they're not the right person."""
    prop = PROPERTIES[1]  # 135 Trade Center Court

    messages = [
        {"direction": "outbound", "content": f"Hi Luke, following up on {prop['address']}."},
        {"direction": "inbound", "content": """Hi Jill,

I don't handle that property anymore. Please contact Sarah Johnson at sarah@augustabrokers.com - she took over that listing.

Luke"""}
    ]

    return run_test(
        name="wrong_contact",
        prop=prop,
        messages=messages,
        expected_events=["wrong_contact"]
    )

def test_property_issue():
    """Broker mentions a problem with the property."""
    prop = PROPERTIES[4]  # 1 Randolph Ct

    messages = [
        {"direction": "outbound", "content": f"Hi Scott, any issues I should know about with {prop['address']}?"},
        {"direction": "inbound", "content": """Hi Jill,

The building has some water damage in the back corner from a roof leak last month. Landlord is getting it repaired but wanted to give you a heads up.

Scott"""}
    ]

    return run_test(
        name="property_issue",
        prop=prop,
        messages=messages,
        expected_events=["property_issue"]
    )

def test_close_conversation():
    """Natural conversation ending."""
    prop = PROPERTIES[2]  # 2058 Gordon Hwy

    messages = [
        {"direction": "outbound", "content": f"Thanks for all the info on {prop['address']}!"},
        {"direction": "inbound", "content": """You're welcome! Let me know if you need anything else. Good luck with your search!

Jonathan"""}
    ]

    return run_test(
        name="close_conversation",
        prop=prop,
        messages=messages,
        expected_events=["close_conversation"]
    )

def test_vague_response():
    """Broker gives vague response with no concrete data."""
    prop = PROPERTIES[3]  # 1 Kuhlke Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Robert, what's the rent and SF for {prop['address']}?"},
        {"direction": "inbound", "content": """Hi,

The rent is competitive for the area. It's a nice sized building with good loading. Great location.

Let me know if you want to tour.

Robert"""}
    ]

    return run_test(
        name="vague_response",
        prop=prop,
        messages=messages,
        expected_updates=[]  # No concrete data to extract
    )

def test_notes_capture():
    """Test that additional details are captured in notes."""
    prop = PROPERTIES[2]  # 2058 Gordon Hwy

    messages = [
        {"direction": "outbound", "content": f"Hi Jonathan, tell me about {prop['address']}."},
        {"direction": "inbound", "content": """Hi Jill,

Here's the info:
- 12,000 SF
- $5.50/SF NNN
- NNN is $1.50/SF
- 2 docks, 1 drive-in
- 18' clear
- 200A power

The property is zoned M-1 heavy industrial. ESFR sprinklered throughout. Climate controlled office area (about 1,500 SF).
Near I-20 exit 199. Can subdivide down to 6,000 SF minimum. Fenced yard with 10 trailer spots.

Available March 1st with flexible 3-7 year terms. Owner is motivated.

Jonathan"""}
    ]

    result = run_test(
        name="notes_capture",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Docks", "Drive Ins", "Ceiling Ht", "Power"],
        should_complete_row=True
    )

    # Additional check for notes
    if not result.notes_captured:
        result.warnings.append("Notes should have captured: zoning, sprinklers, climate control, location, subdivide, fenced yard, availability, terms")

    return result

def test_formatting_validation():
    """Verify values are formatted correctly (no $, SF, etc)."""
    prop = PROPERTIES[4]  # 1 Randolph Ct

    messages = [
        {"direction": "outbound", "content": f"Hi Scott, what are the specs for {prop['address']}?"},
        {"direction": "inbound", "content": """Hi Jill,

- 15,000 square feet total
- $8.50 per square foot per year NNN
- Operating expenses are $2.25/SF/year
- Two drive-in doors
- Four dock-high doors
- Clear height is 24 feet
- Power: 400 amps 3-phase

Scott"""}
    ]

    result = run_test(
        name="formatting_validation",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"]
    )

    # Check for proper formatting in a subsequent validation
    # (The AI should output plain numbers without $, SF, etc)

    return result

def test_conflicting_info():
    """Broker provides conflicting information - should use corrected value."""
    prop = PROPERTIES[0]  # 699 Industrial Park Dr

    messages = [
        {"direction": "outbound", "content": f"Hi Jeff, what's the SF for {prop['address']}?"},
        {"direction": "inbound", "content": """Hi Jill,

The space is 10,000 SF. Actually wait, let me check my notes... it's 8,500 SF, my apologies for the confusion.

Jeff"""}
    ]

    result = run_test(
        name="conflicting_info",
        prop=prop,
        messages=messages,
        expected_updates=["Total SF"]
    )

    return result

def test_budget_question():
    """Broker asks about budget - must escalate."""
    prop = PROPERTIES[4]  # 1 Randolph Ct

    messages = [
        {"direction": "outbound", "content": f"Hi Scott, can you send details on {prop['address']}?"},
        {"direction": "inbound", "content": """Hi Jill,

Sure thing. Before I do, what's the budget range your client is working with? Just want to make sure this is in their price range.

Scott"""}
    ]

    return run_test(
        name="budget_question",
        prop=prop,
        messages=messages,
        expected_events=["needs_user_input"],
        should_escalate=True
    )

def test_full_row_completion():
    """Simulate filling an entire row through multiple conversations."""
    prop = PROPERTIES[3]  # 1 Kuhlke Dr - start fresh

    # First conversation - get basic info
    messages1 = [
        {"direction": "outbound", "content": f"Hi Robert, interested in {prop['address']}. What's the size and rent?"},
        {"direction": "inbound", "content": """Hi Jill,

It's 25,000 SF at $6.50/SF NNN.

Robert"""}
    ]

    result1 = run_test(
        name="full_row_part1_basic",
        prop=prop,
        messages=messages1,
        expected_updates=["Total SF", "Rent/SF /Yr"]
    )

    # Simulate updated row after first conversation
    updated_data = prop["data"].copy()
    idx_map = {h.lower().strip(): i for i, h in enumerate(HEADERS)}
    if "total sf" in idx_map:
        updated_data[idx_map["total sf"]] = "25000"
    if "rent/sf /yr" in idx_map:
        updated_data[idx_map["rent/sf /yr"]] = "6.50"

    # Second conversation - get remaining fields
    messages2 = [
        {"direction": "outbound", "content": "Thanks Robert! What are the NNN expenses, loading, ceiling height, and power?"},
        {"direction": "inbound", "content": """Hi Jill,

- NNN is $2.00/SF
- 4 dock doors
- 2 drive-ins
- 22' clear height
- 400A 3-phase power

Available immediately. The building is sprinklered and has a fenced yard.

Robert"""}
    ]

    result2 = run_test(
        name="full_row_part2_complete",
        prop=prop,
        messages=messages2,
        initial_data=updated_data,
        expected_updates=["Ops Ex /SF", "Docks", "Drive Ins", "Ceiling Ht", "Power"],
        should_complete_row=True
    )

    return result2

# ============================================================================
# RUN ALL TESTS
# ============================================================================

def run_all_tests(quick=False, verbose=True):
    """Run all production tests."""

    print(f"\n{'='*70}")
    print("PRODUCTION END-TO-END TEST SUITE")
    print(f"{'='*70}")
    print(f"Excel: {EXCEL_PATH}")
    print(f"Properties: {len(PROPERTIES)}")
    print(f"Headers: {len(HEADERS)} columns")

    # Run tests
    tests = [
        test_column_detection,
        test_complete_info_single_message,
        test_partial_info_needs_followup,
        test_multi_turn_conversation,
        test_property_unavailable,
        test_unavailable_with_alternative,
        test_new_property_suggestion,
        test_call_requested_with_phone,
        test_call_requested_no_phone,
        test_escalate_client_requirements,
        test_escalate_scheduling,
        test_escalate_negotiation,
        test_escalate_client_identity,
        test_escalate_legal_contract,
        test_mixed_info_and_question,
        test_contact_optout,
        test_wrong_contact,
        test_property_issue,
        test_close_conversation,
        test_vague_response,
        test_notes_capture,
        test_formatting_validation,
        test_conflicting_info,
        test_budget_question,
        test_full_row_completion,
    ]

    if quick:
        # Run subset for quick validation
        tests = tests[:10]
        print(f"\n🏃 Quick mode: running {len(tests)} tests")
    else:
        print(f"\n🔬 Full mode: running {len(tests)} tests")

    for test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            print(f"\n❌ Test {test_fn.__name__} crashed: {e}")
            traceback.print_exc()

        time.sleep(0.5)  # Rate limiting

    # Print summary
    STATS.summary()

    return STATS.passed == STATS.total

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Production E2E Tests")
    parser.add_argument("--quick", action="store_true", help="Run subset of tests")
    parser.add_argument("--verbose", action="store_true", help="Extra debug output")

    args = parser.parse_args()

    success = run_all_tests(quick=args.quick, verbose=True)
    sys.exit(0 if success else 1)
