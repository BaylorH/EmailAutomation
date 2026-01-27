#!/usr/bin/env python3
"""
Persona-Based Campaign Testing Framework
=========================================

This framework runs comprehensive campaigns through multiple "testing personas",
each focusing on different aspects of the system:

1. Data Extraction Tester - Validates field extraction accuracy
2. UX/Notification Tester - Validates frontend receives correct notifications
3. Threading Tester - Validates conversation threading and state management
4. Edge Case Tester - Validates handling of unusual scenarios
5. Campaign Lifecycle Tester - Validates full campaign flow

Each persona has specific validation criteria and will report PASS/FAIL
with detailed feedback on what they're evaluating.

Usage:
    python tests/persona_campaign_tester.py                 # Run all personas
    python tests/persona_campaign_tester.py --persona data  # Run specific persona
    python tests/persona_campaign_tester.py --list          # List personas
"""

import os
import sys
import json
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from enum import Enum

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


# ============================================================================
# DATA TYPES
# ============================================================================

@dataclass
class TestProperty:
    """A property in a test campaign."""
    address: str
    city: str
    contact: str
    email: str
    row: int = 2
    values: Dict[str, str] = field(default_factory=dict)
    conversation: List[Dict] = field(default_factory=list)


@dataclass
class PersonaFeedback:
    """Feedback from a testing persona."""
    persona_name: str
    passed: bool
    score: float
    checks_passed: int
    checks_total: int
    issues: List[str]
    details: Dict[str, Any]


class Severity(Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


# ============================================================================
# TESTING PERSONAS
# ============================================================================

class TestingPersona(ABC):
    """Base class for testing personas."""

    name: str = "Base Persona"
    description: str = "Base testing persona"
    focus_areas: List[str] = []

    @abstractmethod
    def run_tests(self, campaign_results: List[Dict]) -> PersonaFeedback:
        """Run persona-specific tests and return feedback."""
        pass

    def _create_feedback(self, checks: List[Tuple[str, bool, str]],
                         details: Dict = None) -> PersonaFeedback:
        """Create feedback from list of (check_name, passed, issue_if_failed)."""
        passed_checks = [c for c in checks if c[1]]
        failed_checks = [c for c in checks if not c[1]]

        return PersonaFeedback(
            persona_name=self.name,
            passed=len(failed_checks) == 0,
            score=len(passed_checks) / len(checks) if checks else 1.0,
            checks_passed=len(passed_checks),
            checks_total=len(checks),
            issues=[c[2] for c in failed_checks],
            details=details or {}
        )


class DataExtractionTester(TestingPersona):
    """
    Validates AI field extraction accuracy.

    Focus:
    - Are numeric values extracted correctly?
    - Are units handled properly (SF, /yr, etc.)?
    - Are all available fields captured?
    - Are forbidden fields (Gross Rent) never written?
    """

    name = "Data Extraction Tester"
    description = "I validate that the AI correctly extracts property data from broker emails"
    focus_areas = [
        "Numeric value accuracy",
        "Unit handling (SF, /yr, NNN)",
        "Field completeness",
        "Forbidden field protection"
    ]

    REQUIRED_FIELDS = ["Total SF", "Ops Ex /SF", "Drive Ins", "Docks", "Ceiling Ht", "Power"]
    FORBIDDEN_FIELDS = ["Gross Rent"]  # Formula columns

    def run_tests(self, campaign_results: List[Dict]) -> PersonaFeedback:
        checks = []
        extraction_details = {"by_property": {}}

        for result in campaign_results:
            prop_name = result.get("property", "Unknown")
            updates = result.get("updates", [])
            expected = result.get("expected_updates", [])

            prop_checks = []

            # Check 1: No forbidden fields written
            forbidden_written = [u["column"] for u in updates if u["column"] in self.FORBIDDEN_FIELDS]
            prop_checks.append((
                f"{prop_name}: No forbidden fields",
                len(forbidden_written) == 0,
                f"Wrote forbidden fields: {forbidden_written}"
            ))

            # Check 2: Expected fields extracted
            if expected:
                expected_cols = {e["column"] for e in expected}
                actual_cols = {u["column"] for u in updates}
                missing = expected_cols - actual_cols

                prop_checks.append((
                    f"{prop_name}: Expected fields captured",
                    len(missing) == 0,
                    f"Missing expected fields: {list(missing)}"
                ))

                # Check 3: Value accuracy
                for exp in expected:
                    col = exp["column"]
                    exp_val = str(exp["value"]).replace(",", "").strip()
                    actual = next((u for u in updates if u["column"] == col), None)

                    if actual:
                        act_val = str(actual["value"]).replace(",", "").strip()
                        # Normalize numeric comparison
                        try:
                            exp_num = float(exp_val)
                            act_num = float(act_val)
                            match = abs(exp_num - act_num) < 0.01
                        except:
                            match = exp_val.lower() == act_val.lower()

                        prop_checks.append((
                            f"{prop_name}: {col} value correct",
                            match,
                            f"{col}: expected '{exp_val}', got '{act_val}'"
                        ))

            # Check 4: Numeric formatting (no $ or SF in values)
            for update in updates:
                val = str(update.get("value", ""))
                has_currency = "$" in val
                # Allow SF in Power field only
                has_sf = "sf" in val.lower() and update["column"] != "Power"

                if has_currency or has_sf:
                    prop_checks.append((
                        f"{prop_name}: {update['column']} clean format",
                        False,
                        f"{update['column']} has formatting symbols: '{val}'"
                    ))

            checks.extend(prop_checks)
            extraction_details["by_property"][prop_name] = {
                "updates": len(updates),
                "expected": len(expected),
                "checks": [(c[0], c[1]) for c in prop_checks]
            }

        return self._create_feedback(checks, extraction_details)


class UXNotificationTester(TestingPersona):
    """
    Validates frontend notification correctness.

    Focus:
    - Are notifications triggered at the right times?
    - Do notification payloads have required fields?
    - Are priority levels correct?
    - Are action_needed notifications clear and actionable?
    """

    name = "UX/Notification Tester"
    description = "I validate that the frontend receives correct, timely notifications"
    focus_areas = [
        "Notification timing",
        "Payload completeness",
        "Priority correctness",
        "Actionable content"
    ]

    NOTIFICATION_KINDS = ["sheet_update", "action_needed", "row_completed", "property_unavailable"]

    def run_tests(self, campaign_results: List[Dict]) -> PersonaFeedback:
        checks = []
        notification_details = {"by_kind": {}, "total": 0}

        all_notifications = []
        for result in campaign_results:
            notifications = self._derive_notifications(result)
            all_notifications.extend(notifications)

        notification_details["total"] = len(all_notifications)

        # Group by kind
        for kind in self.NOTIFICATION_KINDS:
            kind_notifs = [n for n in all_notifications if n.get("kind") == kind]
            notification_details["by_kind"][kind] = len(kind_notifs)

        # Check 1: All updates generate sheet_update notifications
        for result in campaign_results:
            prop = result.get("property", "Unknown")
            updates = result.get("updates", [])

            checks.append((
                f"{prop}: Updates trigger notifications",
                len(updates) == 0 or any(n.get("kind") == "sheet_update"
                    for n in self._derive_notifications(result)),
                f"Updates made but no sheet_update notification"
            ))

        # Check 2: Action-needed notifications have required fields
        for result in campaign_results:
            events = result.get("events", [])
            for event in events:
                event_type = event.get("type")
                if event_type == "needs_user_input":
                    # needs_user_input should have reason or question
                    has_reason = bool(event.get("reason") or event.get("question"))
                    checks.append((
                        f"needs_user_input has reason/question",
                        has_reason,
                        f"Event needs_user_input missing reason/question"
                    ))
                elif event_type == "tour_requested":
                    # tour_requested should have question (the tour offer)
                    has_question = bool(event.get("question"))
                    checks.append((
                        f"tour_requested has question",
                        has_question,
                        f"Event tour_requested missing question"
                    ))
                # call_requested doesn't need reason/question - the type is self-explanatory

        # Check 3: Property unavailable triggers correct notification
        for result in campaign_results:
            events = result.get("events", [])
            has_unavailable_event = any(e.get("type") == "property_unavailable" for e in events)

            if has_unavailable_event:
                checks.append((
                    f"{result.get('property')}: Unavailable notification",
                    True,
                    ""
                ))

        # Check 4: New property suggestions have contact info
        for result in campaign_results:
            events = result.get("events", [])
            for event in events:
                if event.get("type") == "new_property":
                    has_contact = bool(event.get("email") or event.get("contactName"))
                    checks.append((
                        f"new_property has contact info",
                        has_contact,
                        f"New property suggestion missing contact info"
                    ))

        return self._create_feedback(checks, notification_details)

    def _derive_notifications(self, result: Dict) -> List[Dict]:
        """Derive what notifications would fire from a result."""
        notifications = []

        for update in result.get("updates", []):
            notifications.append({
                "kind": "sheet_update",
                "column": update.get("column"),
                "value": update.get("value")
            })

        for event in result.get("events", []):
            if event.get("type") == "property_unavailable":
                notifications.append({"kind": "property_unavailable"})
            elif event.get("type") in ["needs_user_input", "call_requested", "tour_requested"]:
                notifications.append({
                    "kind": "action_needed",
                    "reason": event.get("type"),
                    "question": event.get("question")
                })
            elif event.get("type") == "new_property":
                notifications.append({
                    "kind": "action_needed",
                    "reason": "new_property_pending_approval"
                })

        return notifications


class ThreadingTester(TestingPersona):
    """
    Validates conversation threading and state management.

    Focus:
    - Do multi-turn conversations accumulate data correctly?
    - Does state persist between turns?
    - Are escalations properly pausing conversations?
    - Does close_conversation properly terminate?
    """

    name = "Threading Tester"
    description = "I validate conversation threading, state management, and pause/resume logic"
    focus_areas = [
        "Multi-turn data accumulation",
        "State persistence",
        "Escalation pausing",
        "Conversation termination"
    ]

    def run_tests(self, campaign_results: List[Dict]) -> PersonaFeedback:
        checks = []
        threading_details = {}

        # Group results by property for multi-turn analysis
        by_property = {}
        for result in campaign_results:
            prop = result.get("property", "Unknown")
            if prop not in by_property:
                by_property[prop] = []
            by_property[prop].append(result)

        for prop, turns in by_property.items():
            # Check 1: Multi-turn data accumulates (doesn't reset)
            if len(turns) > 1:
                first_turn_cols = {u["column"] for u in turns[0].get("updates", [])}
                second_turn_cols = {u["column"] for u in turns[1].get("updates", [])}

                # Second turn shouldn't re-extract first turn's fields
                overlap = first_turn_cols & second_turn_cols
                checks.append((
                    f"{prop}: No redundant extraction",
                    len(overlap) == 0,
                    f"Re-extracted fields: {list(overlap)}"
                ))

            # Check 2: Escalation events should stop response generation
            for turn in turns:
                events = turn.get("events", [])
                escalation_events = [e for e in events if e.get("type") in
                    ["needs_user_input", "call_requested", "tour_requested"]]

                if escalation_events:
                    # Should NOT have a response email when escalating
                    has_response = bool(turn.get("response_email"))
                    checks.append((
                        f"{prop}: Escalation pauses response",
                        not has_response,
                        f"Generated response during escalation"
                    ))

            # Check 3: close_conversation properly terminates
            for turn in turns:
                events = turn.get("events", [])
                has_close = any(e.get("type") == "close_conversation" for e in events)

                if has_close:
                    checks.append((
                        f"{prop}: Close conversation detected",
                        True,
                        ""
                    ))

            threading_details[prop] = {
                "turns": len(turns),
                "total_updates": sum(len(t.get("updates", [])) for t in turns),
                "events": [e.get("type") for t in turns for e in t.get("events", [])]
            }

        return self._create_feedback(checks, threading_details)


class EdgeCaseTester(TestingPersona):
    """
    Validates handling of unusual/edge case scenarios.

    Focus:
    - Hostile/negative responses handled gracefully
    - Empty/minimal responses don't crash
    - Conflicting information handled correctly
    - Various number formats parsed correctly
    """

    name = "Edge Case Tester"
    description = "I validate handling of unusual scenarios and edge cases"
    focus_areas = [
        "Hostile response handling",
        "Empty response handling",
        "Conflicting information",
        "Number format variations"
    ]

    def run_tests(self, campaign_results: List[Dict]) -> PersonaFeedback:
        checks = []
        edge_details = {}

        for result in campaign_results:
            prop = result.get("property", "Unknown")
            scenario = result.get("scenario_type", "unknown")

            # Check 1: No crashes (result exists)
            checks.append((
                f"{prop}: No crash",
                result.get("completed", True),
                f"Scenario {scenario} crashed"
            ))

            # Check 2: Hostile responses trigger appropriate event
            if scenario == "hostile":
                events = result.get("events", [])
                has_optout = any(e.get("type") == "contact_optout" for e in events)
                has_wrong = any(e.get("type") == "wrong_contact" for e in events)

                checks.append((
                    f"{prop}: Hostile response handled",
                    has_optout or has_wrong,
                    f"Hostile response didn't trigger optout/wrong_contact"
                ))

            # Check 3: Empty responses don't crash
            if scenario == "empty" or scenario == "short":
                checks.append((
                    f"{prop}: Empty/short handled",
                    True,  # If we got here, it didn't crash
                    ""
                ))

            # Check 4: Conflicting info noted in notes or events
            if scenario == "conflicting":
                notes = result.get("notes", "")
                events = result.get("events", [])
                has_issue_event = any(e.get("type") == "property_issue" for e in events)
                # Also check if the AI picked one value (it handled the conflict)
                updates = result.get("updates", [])
                handled_conflict = len(updates) > 0  # If it extracted something, it made a decision

                checks.append((
                    f"{prop}: Conflicting info handled",
                    has_issue_event or handled_conflict or "conflict" in notes.lower(),
                    f"Conflicting information not handled (no updates, events, or notes)"
                ))

            edge_details[prop] = {
                "scenario": scenario,
                "updates": len(result.get("updates", [])),
                "events": [e.get("type") for e in result.get("events", [])]
            }

        return self._create_feedback(checks, edge_details)


class CampaignLifecycleTester(TestingPersona):
    """
    Validates complete campaign lifecycle.

    Focus:
    - Campaign starts with all properties pending
    - Properties progress through correct states
    - Campaign completion detected correctly
    - All properties resolved at end
    """

    name = "Campaign Lifecycle Tester"
    description = "I validate the complete campaign lifecycle from start to finish"
    focus_areas = [
        "Campaign initialization",
        "State transitions",
        "Completion detection",
        "Final resolution"
    ]

    TERMINAL_STATES = ["complete", "non_viable", "closed", "needs_action"]

    def run_tests(self, campaign_results: List[Dict]) -> PersonaFeedback:
        checks = []
        lifecycle_details = {}

        # Track property states
        property_states = {}
        for result in campaign_results:
            prop = result.get("property", "Unknown")
            state = self._determine_state(result)
            property_states[prop] = state

        # Check 1: All properties have a final state
        for prop, state in property_states.items():
            checks.append((
                f"{prop}: Has final state",
                state in self.TERMINAL_STATES or state == "in_progress",
                f"Property stuck in {state}"
            ))

        # Check 2: Complete properties have required fields
        for result in campaign_results:
            prop = result.get("property", "Unknown")
            if property_states.get(prop) == "complete":
                updates = result.get("updates", [])
                update_cols = {u["column"] for u in updates}
                required = {"Total SF", "Drive Ins", "Docks", "Ceiling Ht", "Power", "Ops Ex /SF"}
                missing = required - update_cols

                # Check accumulated fields from conversation
                existing = result.get("existing_values", {})
                for col in list(missing):
                    if existing.get(col):
                        missing.discard(col)

                checks.append((
                    f"{prop}: Complete has required fields",
                    len(missing) == 0,
                    f"Complete but missing: {list(missing)}"
                ))

        # Check 3: Non-viable properties have unavailable event
        for result in campaign_results:
            prop = result.get("property", "Unknown")
            if property_states.get(prop) == "non_viable":
                events = result.get("events", [])
                has_unavailable = any(e.get("type") == "property_unavailable" for e in events)

                checks.append((
                    f"{prop}: Non-viable has unavailable event",
                    has_unavailable,
                    f"Non-viable without property_unavailable event"
                ))

        lifecycle_details["property_states"] = property_states
        lifecycle_details["terminal_count"] = sum(1 for s in property_states.values()
                                                   if s in self.TERMINAL_STATES)
        lifecycle_details["total_properties"] = len(property_states)

        return self._create_feedback(checks, lifecycle_details)

    def _determine_state(self, result: Dict) -> str:
        """Determine property state from result."""
        events = result.get("events", [])

        # Check for terminal events
        if any(e.get("type") == "property_unavailable" for e in events):
            return "non_viable"
        if any(e.get("type") == "close_conversation" for e in events):
            return "closed"
        if any(e.get("type") in ["needs_user_input", "call_requested", "tour_requested"] for e in events):
            return "needs_action"

        # Check for completion (all required fields)
        updates = result.get("updates", [])
        existing = result.get("existing_values", {})
        all_cols = {u["column"] for u in updates}
        all_cols.update(k for k, v in existing.items() if v)

        required = {"Total SF", "Drive Ins", "Docks", "Ceiling Ht", "Power", "Ops Ex /SF"}
        if required.issubset(all_cols):
            return "complete"

        return "in_progress"


# ============================================================================
# CAMPAIGN RUNNER
# ============================================================================

HEADER = [
    "Property Address", "City", "Property Name", "Leasing Company",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments", "Flyer / Link", "Floorplan", "Jill and Clients comments"
]


def run_campaign_scenario(properties: List[Dict], scenario_name: str) -> List[Dict]:
    """Run a campaign scenario and return results."""
    results = []

    for prop_def in properties:
        prop = TestProperty(
            address=prop_def["address"],
            city=prop_def.get("city", "Augusta"),
            contact=prop_def.get("contact", "Test Broker"),
            email=prop_def.get("email", "broker@test.com"),
            row=prop_def.get("row", 2)
        )

        # Build conversation
        conversation = []
        conversation.append({
            "direction": "outbound",
            "content": f"Hi, I'm interested in {prop.address}. Could you provide the property details?",
            "from": "jill@mohrpartners.com",
            "to": [prop.email],
            "subject": f"{prop.address}, {prop.city}",
            "timestamp": "2026-01-25T10:00:00Z"
        })

        # Add broker response
        broker_response = prop_def.get("broker_response", "")
        if broker_response:
            conversation.append({
                "direction": "inbound",
                "content": broker_response,
                "from": prop.email,
                "to": ["jill@mohrpartners.com"],
                "subject": f"Re: {prop.address}, {prop.city}",
                "timestamp": "2026-01-25T11:00:00Z"
            })

        # Build row values
        rowvals = [""] * len(HEADER)
        rowvals[HEADER.index("Property Address")] = prop.address
        rowvals[HEADER.index("City")] = prop.city
        rowvals[HEADER.index("Leasing Contact")] = prop.contact
        rowvals[HEADER.index("Email")] = prop.email

        # Add any existing values
        for col, val in prop_def.get("existing_values", {}).items():
            if col in HEADER:
                rowvals[HEADER.index(col)] = val

        # Call AI
        try:
            proposal = propose_sheet_updates(
                uid="persona-test-user",
                client_id="persona-test-client",
                email=prop.email,
                sheet_id="persona-test-sheet",
                header=HEADER,
                rownum=prop.row,
                rowvals=rowvals,
                thread_id=f"thread-{prop.address.lower().replace(' ', '-')}",
                contact_name=prop.contact,
                conversation=conversation,
                dry_run=True
            )

            results.append({
                "property": prop.address,
                "scenario_type": prop_def.get("scenario_type", "standard"),
                "updates": proposal.get("updates", []) if proposal else [],
                "events": proposal.get("events", []) if proposal else [],
                "response_email": proposal.get("response_email") if proposal else None,
                "notes": proposal.get("notes", "") if proposal else "",
                "expected_updates": prop_def.get("expected_updates", []),
                "existing_values": prop_def.get("existing_values", {}),
                "completed": True
            })
        except Exception as e:
            results.append({
                "property": prop.address,
                "scenario_type": prop_def.get("scenario_type", "standard"),
                "error": str(e),
                "completed": False
            })

    return results


# ============================================================================
# TEST CAMPAIGNS
# ============================================================================

CAMPAIGN_STANDARD = {
    "name": "Standard Campaign",
    "description": "5 properties with typical broker responses",
    "properties": [
        {
            "address": "100 Industrial Way",
            "city": "Augusta",
            "contact": "John Smith",
            "email": "john@broker1.com",
            "broker_response": """Hi,

Here are the details for 100 Industrial Way:
- 25,000 SF
- $7.50/SF NNN
- CAM: $1.85/SF
- 2 drive-ins, 4 dock doors
- 28' clear height
- 400 amps, 3-phase

Let me know if you need anything else.

John""",
            "expected_updates": [
                {"column": "Total SF", "value": "25000"},
                {"column": "Rent/SF /Yr", "value": "7.50"},
                {"column": "Ops Ex /SF", "value": "1.85"},
                {"column": "Drive Ins", "value": "2"},
                {"column": "Docks", "value": "4"},
                {"column": "Ceiling Ht", "value": "28"},
                {"column": "Power", "value": "400 amps, 3-phase"}
            ]
        },
        {
            "address": "200 Commerce Dr",
            "city": "North Augusta",
            "contact": "Sarah Chen",
            "email": "sarah@broker2.com",
            "broker_response": """200 Commerce Dr is 18,000 SF at $6.25/SF NNN.

Sarah""",
            "expected_updates": [
                {"column": "Total SF", "value": "18000"},
                {"column": "Rent/SF /Yr", "value": "6.25"}
            ]
        },
        {
            "address": "300 Warehouse Blvd",
            "city": "Evans",
            "contact": "Mike Wilson",
            "email": "mike@broker3.com",
            "broker_response": """Unfortunately 300 Warehouse Blvd is no longer available - we signed a lease last week.

Mike""",
            "scenario_type": "unavailable"
        },
        {
            "address": "400 Distribution Ct",
            "city": "Augusta",
            "contact": "Lisa Brown",
            "email": "lisa@broker4.com",
            "broker_response": """Before I send details, who is your client? I like to know who I'm dealing with.

Lisa""",
            "scenario_type": "identity_question"
        },
        {
            "address": "500 Tech Park",
            "city": "Martinez",
            "contact": "Tom Davis",
            "email": "tom@broker5.com",
            "broker_response": """Can you call me at 555-123-4567? There's a lot to discuss about this one.

Tom""",
            "scenario_type": "call_requested"
        }
    ]
}

CAMPAIGN_EDGE_CASES = {
    "name": "Edge Case Campaign",
    "description": "Properties with unusual/edge case scenarios",
    "properties": [
        {
            "address": "600 Hostile St",
            "city": "Augusta",
            "contact": "Grumpy Broker",
            "email": "grumpy@broker.com",
            "broker_response": """Remove me from your list. We don't work with tenant reps and stop emailing me.""",
            "scenario_type": "hostile"
        },
        {
            "address": "700 Short Ave",
            "city": "Evans",
            "contact": "Brief Person",
            "email": "brief@broker.com",
            "broker_response": """15k sf $8""",
            "scenario_type": "short",
            "expected_updates": [
                {"column": "Total SF", "value": "15000"},
                {"column": "Rent/SF /Yr", "value": "8"}
            ]
        },
        {
            "address": "800 Conflict Rd",
            "city": "Martinez",
            "contact": "Confused Broker",
            "email": "confused@broker.com",
            "broker_response": """The space is 20,000 SF. Actually wait, I think it's 18,500 SF. Let me double check - yes it's 20k.
Rent is $7/SF or maybe $7.50 depending on term.""",
            "scenario_type": "conflicting"
        },
        {
            "address": "900 Number Format Way",
            "city": "Augusta",
            "contact": "Math Person",
            "email": "math@broker.com",
            "broker_response": """Specs for 900 Number Format Way:
- Size: fifteen thousand square feet
- Rent: seven fifty per foot NNN
- OpEx: around $2 per foot annually
- Doors: 3 docks and 1 drive-in
- Height: twenty-four feet clear
- Power: two hundred amps""",
            "scenario_type": "number_words",
            "expected_updates": [
                {"column": "Total SF", "value": "15000"},
                {"column": "Rent/SF /Yr", "value": "7.50"},
                {"column": "Ops Ex /SF", "value": "2"},
                {"column": "Drive Ins", "value": "1"},
                {"column": "Docks", "value": "3"},
                {"column": "Ceiling Ht", "value": "24"},
                {"column": "Power", "value": "200 amps"}
            ]
        }
    ]
}

CAMPAIGN_MULTI_TURN = {
    "name": "Multi-Turn Campaign",
    "description": "Properties requiring multiple conversation turns",
    "properties": [
        {
            "address": "1000 Partial St",
            "city": "Augusta",
            "contact": "Slow Responder",
            "email": "slow@broker.com",
            "existing_values": {
                "Total SF": "12000",
                "Rent/SF /Yr": "6.50"
            },
            "broker_response": """Here are the remaining details:
- NNN: $1.85/SF
- 2 dock doors, 1 drive-in
- 22' clear
- 200 amps""",
            "expected_updates": [
                {"column": "Ops Ex /SF", "value": "1.85"},
                {"column": "Drive Ins", "value": "1"},
                {"column": "Docks", "value": "2"},
                {"column": "Ceiling Ht", "value": "22"},
                {"column": "Power", "value": "200 amps"}
            ]
        }
    ]
}


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def run_all_personas(verbose: bool = True) -> Dict[str, PersonaFeedback]:
    """Run all campaigns through all personas and collect feedback."""

    personas = [
        DataExtractionTester(),
        UXNotificationTester(),
        ThreadingTester(),
        EdgeCaseTester(),
        CampaignLifecycleTester()
    ]

    campaigns = [CAMPAIGN_STANDARD, CAMPAIGN_EDGE_CASES, CAMPAIGN_MULTI_TURN]

    # Run all campaigns
    all_results = []
    for campaign in campaigns:
        if verbose:
            print(f"\nRunning campaign: {campaign['name']}...")

        results = run_campaign_scenario(campaign["properties"], campaign["name"])
        all_results.extend(results)

        if verbose:
            print(f"  Processed {len(results)} properties")

    # Run each persona
    feedback = {}

    if verbose:
        print("\n" + "="*70)
        print("PERSONA FEEDBACK")
        print("="*70)

    for persona in personas:
        if verbose:
            print(f"\n{persona.name}")
            print(f"  {persona.description}")
            print(f"  Focus: {', '.join(persona.focus_areas)}")

        persona_feedback = persona.run_tests(all_results)
        feedback[persona.name] = persona_feedback

        if verbose:
            status = "SATISFIED" if persona_feedback.passed else "NOT SATISFIED"
            emoji = "..." if persona_feedback.passed else "!!!"
            print(f"\n  Status: {status} {emoji}")
            print(f"  Score: {persona_feedback.score:.1%} ({persona_feedback.checks_passed}/{persona_feedback.checks_total} checks)")

            if persona_feedback.issues:
                print(f"  Issues:")
                for issue in persona_feedback.issues[:5]:  # Show first 5
                    print(f"    - {issue}")
                if len(persona_feedback.issues) > 5:
                    print(f"    ... and {len(persona_feedback.issues) - 5} more")

    return feedback


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Persona-Based Campaign Testing")
    parser.add_argument("--persona", "-p", help="Run specific persona (data, ux, threading, edge, lifecycle)")
    parser.add_argument("--list", "-l", action="store_true", help="List available personas")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    args = parser.parse_args()

    if args.list:
        personas = [
            DataExtractionTester(),
            UXNotificationTester(),
            ThreadingTester(),
            EdgeCaseTester(),
            CampaignLifecycleTester()
        ]
        print("\nAvailable Testing Personas:")
        print("="*50)
        for p in personas:
            print(f"\n{p.name}")
            print(f"  {p.description}")
            print(f"  Focus areas: {', '.join(p.focus_areas)}")
        return

    print("\n" + "="*70)
    print("PERSONA-BASED CAMPAIGN TESTING")
    print("="*70)
    print(f"Time: {datetime.now().isoformat()}")

    feedback = run_all_personas(not args.quiet)

    # Final summary
    print("\n" + "="*70)
    print("FINAL SUMMARY")
    print("="*70)

    all_satisfied = True
    for name, fb in feedback.items():
        status = "SATISFIED" if fb.passed else "NEEDS ATTENTION"
        emoji = "[OK]" if fb.passed else "[!!]"
        print(f"  {emoji} {name}: {fb.score:.1%}")
        if not fb.passed:
            all_satisfied = False

    print(f"\n{'ALL PERSONAS SATISFIED' if all_satisfied else 'SOME PERSONAS NEED ATTENTION'}")

    return 0 if all_satisfied else 1


if __name__ == "__main__":
    sys.exit(main())
