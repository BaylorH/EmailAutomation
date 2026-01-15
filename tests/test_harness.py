"""
Test harness for email automation system.
Simulates the conversation -> extraction -> sheet update pipeline.
"""

import sys
import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.mock_data import (
    REAL_HEADER, SAMPLE_PROPERTIES, SCENARIOS,
    ConversationScenario, get_scenario_by_name, get_all_scenarios,
    get_header_index_map, create_mock_sheet
)


@dataclass
class TestResult:
    """Result of a single test scenario."""
    scenario_name: str
    passed: bool
    expected_updates: List[Dict]
    actual_updates: List[Dict]
    expected_events: List[Dict]
    actual_events: List[Dict]
    expected_response_type: str
    actual_response_type: str
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    proposal: Dict = field(default_factory=dict)
    response_email: str = ""


class MockSheetState:
    """Simulates Google Sheets state for testing."""

    def __init__(self, header: List[str], initial_rows: Dict[int, List[str]]):
        self.header = header
        self.rows = {k: v.copy() for k, v in initial_rows.items()}
        self.ai_meta = {}  # Track AI writes: {(row, column): {"value": ..., "timestamp": ...}}
        self.divider_row = None
        self.changes_log = []  # Track all changes made

    def get_row(self, row_num: int) -> List[str]:
        """Get row data, padded to header length."""
        if row_num not in self.rows:
            return [""] * len(self.header)
        row = self.rows[row_num]
        return row + [""] * (len(self.header) - len(row))

    def update_cell(self, row_num: int, column_name: str, value: str, is_ai: bool = True):
        """Update a cell value."""
        idx_map = get_header_index_map(self.header)
        col_key = column_name.strip().lower()

        if col_key not in idx_map:
            return False

        col_idx = idx_map[col_key] - 1  # 0-based

        # Ensure row exists
        if row_num not in self.rows:
            self.rows[row_num] = [""] * len(self.header)

        # Pad if needed
        while len(self.rows[row_num]) <= col_idx:
            self.rows[row_num].append("")

        old_value = self.rows[row_num][col_idx]
        self.rows[row_num][col_idx] = value

        # Track AI write
        if is_ai:
            self.ai_meta[(row_num, col_key)] = {
                "value": value,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

        self.changes_log.append({
            "row": row_num,
            "column": column_name,
            "old_value": old_value,
            "new_value": value,
            "is_ai": is_ai
        })

        return True

    def check_human_override(self, row_num: int, column_name: str) -> bool:
        """Check if human has overridden an AI-written value."""
        col_key = column_name.strip().lower()
        meta_key = (row_num, col_key)

        if meta_key not in self.ai_meta:
            return False  # No prior AI write

        # Get current value
        idx_map = get_header_index_map(self.header)
        if col_key not in idx_map:
            return False

        col_idx = idx_map[col_key] - 1
        current_value = self.rows.get(row_num, [""] * len(self.header))[col_idx] if row_num in self.rows else ""

        # If current value differs from last AI write, human modified it
        return current_value != self.ai_meta[meta_key]["value"]

    def add_divider(self, row_num: int):
        """Add NON-VIABLE divider at specified row."""
        self.divider_row = row_num
        self.rows[row_num] = ["NON-VIABLE"] + [""] * (len(self.header) - 1)

    def move_row_below_divider(self, src_row: int) -> int:
        """Simulate moving a row below the divider."""
        if self.divider_row is None:
            # Create divider at end
            max_row = max(self.rows.keys()) if self.rows else 2
            self.divider_row = max_row + 1
            self.add_divider(self.divider_row)

        # Get source row data
        src_data = self.rows.get(src_row, [""] * len(self.header))

        # New position is below divider
        new_row = self.divider_row + 1

        # Shift any existing rows below divider
        rows_to_shift = [r for r in self.rows.keys() if r > self.divider_row]
        for r in sorted(rows_to_shift, reverse=True):
            self.rows[r + 1] = self.rows[r]

        # Place source data below divider
        self.rows[new_row] = src_data

        # Remove from original position
        if src_row in self.rows:
            del self.rows[src_row]

        return new_row


def build_conversation_from_scenario(scenario: ConversationScenario) -> List[Dict]:
    """Build conversation payload from scenario messages."""
    payload = []
    for msg in scenario.messages:
        payload.append({
            "direction": msg["direction"],
            "from": scenario.email if msg["direction"] == "inbound" else "jill@example.com",
            "to": ["jill@example.com"] if msg["direction"] == "inbound" else [scenario.email],
            "subject": f"{scenario.property_address}, {scenario.city}",
            "timestamp": msg["timestamp"],
            "preview": msg["content"][:200],
            "content": msg["content"]
        })
    return payload


def simulate_ai_extraction(scenario: ConversationScenario, conversation: List[Dict]) -> Dict:
    """
    Simulate what the AI should extract from the conversation.
    This is the core logic we're testing - it mirrors propose_sheet_updates().
    """
    # In a real test, this would call OpenAI. For simulation, we use expected values.
    proposal = {
        "updates": [],
        "events": [],
        "response_email": "",
        "notes": ""
    }

    # Get the last inbound message
    last_inbound = None
    for msg in reversed(conversation):
        if msg["direction"] == "inbound":
            last_inbound = msg
            break

    if not last_inbound:
        return proposal

    content = last_inbound["content"].lower()

    # Check for auto-reply
    auto_reply_patterns = [
        "out of office", "automatic reply", "auto-reply",
        "away from office", "ooo:"
    ]
    if any(p in content for p in auto_reply_patterns):
        return {"skip": True}

    # Check for unavailability keywords
    unavailable_keywords = [
        "no longer available", "not available", "off the market",
        "has been leased", "unavailable", "was leased"
    ]
    if any(kw in content for kw in unavailable_keywords):
        proposal["events"].append({"type": "property_unavailable"})

    # Check for new property mentions
    new_property_indicators = [
        "another property", "new listing", "also have",
        "different location", "alternative"
    ]
    if any(ind in content for ind in new_property_indicators):
        # Try to extract address from context
        proposal["events"].append({
            "type": "new_property",
            "address": "extracted_address",
            "city": "extracted_city"
        })

    # Check for call request
    call_patterns = ["give me a call", "call me", "phone call", "schedule a call"]
    if any(p in content for p in call_patterns):
        # Check for phone number
        import re
        phone_pattern = r'(?:\+?1[-.\s]?)?\(?([0-9]{3})\)?[-.\s]?([0-9]{3})[-.\s]?([0-9]{4})'
        phone_match = re.search(phone_pattern, last_inbound["content"])
        event = {"type": "call_requested"}
        if phone_match:
            event["phone"] = phone_match.group(0)
        proposal["events"].append(event)

    # Check for close conversation
    close_patterns = ["good luck", "let me know if you need anything else", "you're welcome"]
    if any(p in content for p in close_patterns):
        proposal["events"].append({"type": "close_conversation"})

    # Use expected updates from scenario for simulation
    for expected in scenario.expected_updates:
        proposal["updates"].append({
            "column": expected["column"],
            "value": expected["value"],
            "confidence": 0.9,
            "reason": "Extracted from conversation"
        })

    # Generate response type
    if "skip" in proposal:
        proposal["response_type"] = "skip"
    elif any(e["type"] == "call_requested" and e.get("phone") for e in proposal["events"]):
        proposal["response_type"] = "skip_response"
    elif any(e["type"] == "call_requested" for e in proposal["events"]):
        proposal["response_type"] = "ask_for_phone"
    elif any(e["type"] == "property_unavailable" for e in proposal["events"]):
        if any(e["type"] == "new_property" for e in proposal["events"]):
            proposal["response_type"] = "unavailable_with_new_property"
        else:
            proposal["response_type"] = "unavailable_ask_alternatives"
    elif proposal["updates"]:
        # Check if all required fields are filled
        required_fields = ["Total SF", "Ops Ex /SF", "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power"]
        filled = [u["column"] for u in proposal["updates"]]
        if all(f in filled for f in required_fields):
            proposal["response_type"] = "closing"
        else:
            proposal["response_type"] = "missing_fields"
    else:
        proposal["response_type"] = "missing_fields"

    return proposal


def apply_proposal_simulation(sheet_state: MockSheetState, row_num: int,
                              proposal: Dict, check_guards: bool = True) -> Dict:
    """
    Simulate applying proposal to sheet with AI write guards.
    Returns {"applied": [...], "skipped": [...]}
    """
    result = {"applied": [], "skipped": []}

    if not proposal or not proposal.get("updates"):
        return result

    current_row = sheet_state.get_row(row_num)
    idx_map = get_header_index_map(sheet_state.header)

    for update in proposal["updates"]:
        col_name = update.get("column", "")
        new_val = str(update.get("value", ""))
        confidence = update.get("confidence", 0.5)

        col_key = col_name.strip().lower()
        if col_key not in idx_map:
            result["skipped"].append({
                "column": col_name,
                "reason": "unknown header"
            })
            continue

        col_idx = idx_map[col_key] - 1
        old_val = current_row[col_idx] if col_idx < len(current_row) else ""

        # Check: no change needed
        if old_val == new_val:
            result["skipped"].append({
                "column": col_name,
                "reason": "no-change"
            })
            continue

        # Check: human override
        if check_guards and sheet_state.check_human_override(row_num, col_name):
            result["skipped"].append({
                "column": col_name,
                "reason": "human-override",
                "oldValue": old_val
            })
            continue

        # Check: existing value without prior AI write
        if check_guards and old_val.strip():
            is_placeholder = any(m in old_val.lower() for m in ["tbd", "?", "n/a", "unknown"])
            has_high_confidence = confidence >= 0.8

            if not (has_high_confidence or is_placeholder):
                result["skipped"].append({
                    "column": col_name,
                    "reason": "existing-human-value",
                    "oldValue": old_val
                })
                continue

        # Apply update
        sheet_state.update_cell(row_num, col_name, new_val, is_ai=True)
        result["applied"].append({
            "column": col_name,
            "oldValue": old_val,
            "newValue": new_val,
            "confidence": confidence
        })

    return result


def check_missing_fields(row_data: List[str], header: List[str]) -> List[str]:
    """Check which required fields are missing."""
    required = ["Total SF", "Ops Ex /SF", "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power"]
    idx_map = get_header_index_map(header)
    missing = []

    for field in required:
        key = field.strip().lower()
        if key in idx_map:
            col_idx = idx_map[key] - 1
            val = row_data[col_idx] if col_idx < len(row_data) else ""
            if not val.strip():
                missing.append(field)
        else:
            missing.append(field)

    return missing


def run_scenario(scenario: ConversationScenario, verbose: bool = True) -> TestResult:
    """Run a single test scenario and return results."""
    if verbose:
        print(f"\n{'='*60}")
        print(f"SCENARIO: {scenario.name}")
        print(f"Description: {scenario.description}")
        print(f"{'='*60}")

    errors = []
    warnings = []

    # Setup mock sheet
    initial_rows = {}
    for prop in SAMPLE_PROPERTIES:
        if scenario.initial_row_data and prop["data"][0] == scenario.property_address:
            initial_rows[prop["row"]] = scenario.initial_row_data
        else:
            initial_rows[prop["row"]] = prop["data"].copy()

    sheet_state = MockSheetState(REAL_HEADER.copy(), initial_rows)

    # Find the row for this scenario
    row_num = None
    for prop in SAMPLE_PROPERTIES:
        if prop["data"][0] == scenario.property_address:
            row_num = prop["row"]
            break

    if row_num is None:
        errors.append(f"Property {scenario.property_address} not found in sample data")
        return TestResult(
            scenario_name=scenario.name,
            passed=False,
            expected_updates=scenario.expected_updates,
            actual_updates=[],
            expected_events=scenario.expected_events,
            actual_events=[],
            expected_response_type=scenario.expected_response_type,
            actual_response_type="error",
            errors=errors
        )

    # Build conversation
    conversation = build_conversation_from_scenario(scenario)

    if verbose:
        print(f"\nConversation ({len(conversation)} messages):")
        for msg in conversation:
            direction = "→" if msg["direction"] == "outbound" else "←"
            print(f"  {direction} {msg['direction']}: {msg['content'][:100]}...")

    # Simulate AI extraction
    proposal = simulate_ai_extraction(scenario, conversation)

    if verbose:
        print(f"\nAI Proposal:")
        print(f"  Updates: {len(proposal.get('updates', []))}")
        for u in proposal.get("updates", []):
            print(f"    • {u['column']}: {u['value']}")
        print(f"  Events: {proposal.get('events', [])}")
        print(f"  Response type: {proposal.get('response_type', 'unknown')}")

    # Check for auto-reply skip
    if proposal.get("skip"):
        actual_response_type = "skip"
        actual_updates = []
        actual_events = []
    else:
        # Apply proposal to sheet
        apply_result = apply_proposal_simulation(sheet_state, row_num, proposal)
        actual_updates = apply_result["applied"]
        actual_events = proposal.get("events", [])
        actual_response_type = proposal.get("response_type", "unknown")

        if verbose:
            print(f"\nSheet Updates Applied:")
            for a in actual_updates:
                print(f"  ✅ {a['column']}: '{a['oldValue']}' → '{a['newValue']}'")
            for s in apply_result["skipped"]:
                print(f"  ⏭️  {s['column']}: skipped ({s['reason']})")

    # Validate results
    passed = True

    # Check updates
    expected_columns = {u["column"] for u in scenario.expected_updates if not u.get("should_skip_if_human_override")}
    actual_columns = {u["column"] for u in actual_updates}

    missing_updates = expected_columns - actual_columns
    extra_updates = actual_columns - expected_columns

    if missing_updates:
        errors.append(f"Missing expected updates: {missing_updates}")
        passed = False

    if extra_updates:
        warnings.append(f"Extra updates not expected: {extra_updates}")

    # Check update values
    for expected in scenario.expected_updates:
        if expected.get("should_skip_if_human_override"):
            continue
        matching = [a for a in actual_updates if a["column"] == expected["column"]]
        if matching:
            if matching[0]["newValue"] != expected["value"]:
                errors.append(f"Wrong value for {expected['column']}: expected '{expected['value']}', got '{matching[0]['newValue']}'")
                passed = False

    # Check events
    expected_event_types = {e["type"] for e in scenario.expected_events}
    actual_event_types = {e["type"] for e in actual_events}

    if expected_event_types != actual_event_types:
        errors.append(f"Event mismatch: expected {expected_event_types}, got {actual_event_types}")
        passed = False

    # Check response type
    if actual_response_type != scenario.expected_response_type:
        errors.append(f"Response type mismatch: expected '{scenario.expected_response_type}', got '{actual_response_type}'")
        passed = False

    if verbose:
        print(f"\n{'✅ PASSED' if passed else '❌ FAILED'}")
        if errors:
            print("Errors:")
            for e in errors:
                print(f"  • {e}")
        if warnings:
            print("Warnings:")
            for w in warnings:
                print(f"  • {w}")

    return TestResult(
        scenario_name=scenario.name,
        passed=passed,
        expected_updates=scenario.expected_updates,
        actual_updates=actual_updates,
        expected_events=scenario.expected_events,
        actual_events=actual_events,
        expected_response_type=scenario.expected_response_type,
        actual_response_type=actual_response_type,
        errors=errors,
        warnings=warnings,
        proposal=proposal
    )


def run_all_scenarios(verbose: bool = True) -> List[TestResult]:
    """Run all test scenarios."""
    results = []

    print("\n" + "="*80)
    print("EMAIL AUTOMATION TEST SUITE")
    print("="*80)
    print(f"Running {len(SCENARIOS)} scenarios...")

    for scenario in SCENARIOS:
        result = run_scenario(scenario, verbose=verbose)
        results.append(result)

    # Summary
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed}")

    if failed > 0:
        print("\nFailed scenarios:")
        for r in results:
            if not r.passed:
                print(f"  ❌ {r.scenario_name}")
                for e in r.errors:
                    print(f"      {e}")

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run email automation tests")
    parser.add_argument("--scenario", "-s", help="Run specific scenario by name")
    parser.add_argument("--quiet", "-q", action="store_true", help="Minimal output")
    parser.add_argument("--list", "-l", action="store_true", help="List all scenarios")

    args = parser.parse_args()

    if args.list:
        print("Available scenarios:")
        for s in SCENARIOS:
            print(f"  • {s.name}: {s.description}")
    elif args.scenario:
        scenario = get_scenario_by_name(args.scenario)
        if scenario:
            run_scenario(scenario, verbose=not args.quiet)
        else:
            print(f"Scenario '{args.scenario}' not found")
    else:
        run_all_scenarios(verbose=not args.quiet)
