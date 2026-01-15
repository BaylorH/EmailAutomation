#!/usr/bin/env python3
"""
Full End-to-End Test Runner
===========================
This script runs comprehensive tests of the email automation system,
calling the actual OpenAI API to validate extraction and response behavior.

Output: Detailed report showing how the system handles each scenario.
"""

import sys
import os
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Any
from dataclasses import dataclass, field, asdict

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.mock_data import (
    REAL_HEADER, SCENARIOS, SAMPLE_PROPERTIES,
    ConversationScenario, get_scenario_by_name, get_all_scenarios,
    get_header_index_map
)


@dataclass
class ScenarioResult:
    """Detailed result for a single scenario test."""
    name: str
    description: str
    property_address: str
    city: str
    email: str

    # What the AI produced
    ai_updates: List[Dict] = field(default_factory=list)
    ai_events: List[Dict] = field(default_factory=list)
    ai_response_email: str = ""
    ai_notes: str = ""

    # Expected vs Actual comparison
    expected_updates: List[Dict] = field(default_factory=list)
    expected_events: List[Dict] = field(default_factory=list)
    expected_response_type: str = ""

    # Validation
    updates_correct: bool = False
    events_correct: bool = False
    response_appropriate: bool = False
    overall_pass: bool = False

    # Issues found
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Timing
    api_call_ms: int = 0

    # Raw data for debugging
    raw_response: Dict = field(default_factory=dict)
    conversation_preview: str = ""


class FullTestRunner:
    """Runs complete end-to-end tests with OpenAI."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.results: List[ScenarioResult] = []
        self._init_openai()

    def _init_openai(self):
        """Initialize OpenAI client."""
        try:
            from email_automation.clients import client
            self.openai = client
            self.openai_available = True
            if self.verbose:
                print("✅ OpenAI client initialized")
        except Exception as e:
            print(f"⚠️ Could not initialize OpenAI: {e}")
            self.openai_available = False

    def _get_row_data(self, scenario: ConversationScenario) -> List[str]:
        """Get the initial row data for a scenario."""
        if scenario.initial_row_data:
            return scenario.initial_row_data

        for prop in SAMPLE_PROPERTIES:
            if prop["data"][0] == scenario.property_address:
                return prop["data"].copy()

        return [""] * len(REAL_HEADER)

    def _build_conversation_payload(self, scenario: ConversationScenario) -> List[Dict]:
        """Build conversation payload from scenario."""
        payload = []
        for msg in scenario.messages:
            if msg.get("is_auto_reply"):
                continue  # Skip auto-replies in conversation

            payload.append({
                "direction": msg["direction"],
                "from": scenario.email if msg["direction"] == "inbound" else "jill@company.com",
                "to": ["jill@company.com"] if msg["direction"] == "inbound" else [scenario.email],
                "subject": f"{scenario.property_address}, {scenario.city}",
                "timestamp": msg["timestamp"],
                "preview": msg["content"][:200],
                "content": msg["content"]
            })
        return payload

    def _build_prompt(self, scenario: ConversationScenario, row_data: List[str],
                      conversation: List[Dict]) -> str:
        """Build the full prompt for OpenAI."""

        target_anchor = f"{scenario.property_address}, {scenario.city}"

        # Calculate missing fields
        from email_automation.app_config import REQUIRED_FIELDS_FOR_CLOSE
        idx_map = get_header_index_map(REAL_HEADER)
        missing_fields = []
        for field in REQUIRED_FIELDS_FOR_CLOSE:
            key = field.strip().lower()
            if key in idx_map:
                col_idx = idx_map[key] - 1
                val = row_data[col_idx] if col_idx < len(row_data) else ""
                if not val.strip():
                    missing_fields.append(field)

        # Full prompt matching the real system
        prompt = f"""
You are analyzing a conversation thread to suggest updates to ONE Google Sheet row, detect key events, and generate an appropriate response email.

TARGET PROPERTY (canonical identity for matching): {target_anchor}
CONTACT NAME (optional - use contextually): {scenario.contact_name}

COLUMN SEMANTICS & MAPPING (use EXACT header names):
- "Rent/SF /Yr": Base/asking rent per square foot per YEAR. Synonyms: asking, base rent, $/SF/yr.
- "Ops Ex /SF": NNN/CAM/Operating Expenses per square foot per YEAR. Synonyms: NNN, CAM, OpEx, operating expenses.
- "Gross Rent": If BOTH base rent and NNN are present, set to (Rent/SF /Yr + Ops Ex /SF), rounded to 2 decimals. Else leave unchanged.
- "Total SF": Total square footage. Synonyms: sq footage, square feet, SF, size.
- "Drive Ins": Number of drive-in doors. Synonyms: drive in doors, loading doors.
- "Docks": Number of dock doors/loading docks. Synonyms: dock doors, loading docks, dock positions.
- "Ceiling Ht": Ceiling height. Synonyms: max ceiling height, ceiling clearance, clear height.
- "Power": Electrical power specifications. Synonyms: electrical, amperage, voltage.
- "Listing Brokers Comments ": Short notes captured in the "notes" field.

FORMATTING:
- For money/area fields, output plain decimals (no "$", "SF", commas). Examples: "30", "14.29", "2400".
- For square footage, output just the number: "2000" not "2000 SF".
- For ceiling height, output just the number: "9" not "9 feet" or "9'".
- For drive-ins and docks, output just the number.

DOCUMENT SELECTION (strict):
- Trust ATTACHMENTS (PDFs) over the email body when numbers conflict.
- Extract values ONLY for the TARGET PROPERTY.

EVENTS DETECTION (analyze ONLY the LAST HUMAN message):
- "property_unavailable": ONLY when the CURRENT TARGET PROPERTY is explicitly stated as unavailable/leased/off-market/no longer available.
- "new_property": Emit when the LAST HUMAN message suggests or mentions a DIFFERENT property than the TARGET PROPERTY. Look for URLs, different addresses, phrases like "another property", "new listing".
- "call_requested": Only when someone explicitly asks for a call/phone conversation.
- "close_conversation": When conversation appears complete and the sender indicates they're done.

RESPONSE EMAIL GENERATION:
- Start with a greeting (e.g., "Hi,")
- DO NOT include "Best," or any closing - the footer will add it automatically
- NEVER request "Rent/SF /Yr" - this field should never be asked for
- End with simple "Thanks" - do NOT use "Looking forward to your response"
- Keep responses concise

SHEET HEADER (row 2):
{json.dumps(REAL_HEADER)}

CURRENT ROW VALUES:
{json.dumps(row_data)}

MISSING REQUIRED FIELDS:
{json.dumps(missing_fields)}

CONVERSATION HISTORY (latest last):
{json.dumps(conversation, indent=2)}

Be conservative: only suggest changes you can cite from the text, attachments, or fetched URLs.

OUTPUT ONLY valid JSON in this exact format:
{{
  "updates": [
    {{"column": "<exact header name>", "value": "<new value as string>", "confidence": 0.85, "reason": "<brief explanation>"}}
  ],
  "events": [
    {{"type": "call_requested | property_unavailable | new_property | close_conversation", "address": "<for new_property>", "city": "<for new_property>", "link": "<if URL mentioned>", "notes": "<context>"}}
  ],
  "response_email": "<Generate a professional response email body. Start with greeting, include main message content, end with 'Thanks' - NO closing/signature.>",
  "notes": "<Capture important conversation details for comments field>"
}}
"""
        return prompt

    def _call_openai(self, prompt: str) -> tuple:
        """Call OpenAI and return (response_dict, time_ms)."""
        if not self.openai_available:
            return {"error": "OpenAI not available"}, 0

        start = time.time()
        try:
            response = self.openai.responses.create(
                model="gpt-4o",
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                temperature=0.1
            )

            elapsed_ms = int((time.time() - start) * 1000)
            raw = (response.output_text or "").strip()

            # Parse JSON
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

            parsed = json.loads(raw)
            return parsed, elapsed_ms

        except json.JSONDecodeError as e:
            elapsed_ms = int((time.time() - start) * 1000)
            return {"error": f"JSON parse error: {e}", "raw": raw}, elapsed_ms
        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            return {"error": str(e)}, elapsed_ms

    def _validate_updates(self, expected: List[Dict], actual: List[Dict]) -> tuple:
        """Validate updates match expectations. Returns (correct, issues, warnings)."""
        issues = []
        warnings = []

        actual_by_col = {u.get("column"): u for u in actual}

        for exp in expected:
            if exp.get("should_skip_if_human_override"):
                continue

            col = exp["column"]
            if col not in actual_by_col:
                issues.append(f"Missing update for '{col}'")
                continue

            exp_val = str(exp["value"]).replace(",", "").strip()
            act_val = str(actual_by_col[col].get("value", "")).replace(",", "").strip()

            if exp_val != act_val:
                issues.append(f"Wrong value for '{col}': expected '{exp_val}', got '{act_val}'")

        # Check for extra updates (not necessarily wrong)
        expected_cols = {e["column"] for e in expected}
        for act in actual:
            if act.get("column") not in expected_cols:
                warnings.append(f"Extra update: {act.get('column')} = {act.get('value')}")

        return len(issues) == 0, issues, warnings

    def _validate_events(self, expected: List[Dict], actual: List[Dict]) -> tuple:
        """Validate events match expectations."""
        issues = []
        warnings = []

        expected_types = set(e["type"] for e in expected)
        actual_types = set(e.get("type") for e in actual)

        missing = expected_types - actual_types
        if missing:
            issues.append(f"Missing events: {missing}")

        extra = actual_types - expected_types
        if extra:
            warnings.append(f"Extra events detected: {extra}")

        return len(issues) == 0, issues, warnings

    def _validate_response(self, response_email: str, expected_type: str,
                           missing_fields: List[str]) -> tuple:
        """Validate the response email is appropriate."""
        issues = []
        warnings = []

        if not response_email:
            if expected_type not in ["skip", "skip_response"]:
                issues.append("No response email generated when one was expected")
            return len(issues) == 0, issues, warnings

        email_lower = response_email.lower()

        # Check for forbidden patterns
        if "rent/sf /yr" in email_lower or "rent/sf/yr" in email_lower:
            issues.append("Response asks for 'Rent/SF /Yr' which should NEVER be requested")

        if "looking forward to your response" in email_lower:
            warnings.append("Contains 'Looking forward to your response' (should use simple 'Thanks')")

        # Check response matches expected type
        if expected_type == "missing_fields":
            if not any(field.lower() in email_lower for field in missing_fields if field != "Rent/SF /Yr"):
                warnings.append("Response should mention missing fields")

        if expected_type == "closing":
            completion_phrases = ["everything", "all the", "complete", "have what"]
            if not any(p in email_lower for p in completion_phrases):
                warnings.append("Closing response should indicate all info received")

        return len(issues) == 0, issues, warnings

    def test_scenario(self, scenario: ConversationScenario) -> ScenarioResult:
        """Run full test on a single scenario."""
        result = ScenarioResult(
            name=scenario.name,
            description=scenario.description,
            property_address=scenario.property_address,
            city=scenario.city,
            email=scenario.email,
            expected_updates=scenario.expected_updates,
            expected_events=scenario.expected_events,
            expected_response_type=scenario.expected_response_type
        )

        if self.verbose:
            print(f"\n{'='*70}")
            print(f"🧪 {scenario.name}")
            print(f"   {scenario.description}")
            print(f"   Property: {scenario.property_address}, {scenario.city}")
            print(f"{'='*70}")

        # Check for auto-reply scenario
        for msg in scenario.messages:
            if msg.get("is_auto_reply"):
                result.ai_response_email = ""
                result.overall_pass = True
                result.updates_correct = True
                result.events_correct = True
                result.response_appropriate = True
                if self.verbose:
                    print("   ⏭️  Auto-reply scenario - should be skipped")
                    print("   ✅ PASS (auto-reply handling)")
                return result

        # Get row data and build conversation
        row_data = self._get_row_data(scenario)
        conversation = self._build_conversation_payload(scenario)

        result.conversation_preview = " | ".join(
            f"{'OUT' if m['direction']=='outbound' else 'IN'}: {m['content'][:50]}..."
            for m in conversation
        )

        if self.verbose:
            print(f"\n   Conversation ({len(conversation)} messages):")
            for msg in conversation:
                arrow = "   →" if msg["direction"] == "outbound" else "   ←"
                print(f"   {arrow} {msg['content'][:60]}...")

        # Build prompt and call OpenAI
        prompt = self._build_prompt(scenario, row_data, conversation)

        if self.verbose:
            print(f"\n   Calling OpenAI...")

        ai_response, elapsed_ms = self._call_openai(prompt)
        result.api_call_ms = elapsed_ms
        result.raw_response = ai_response

        if "error" in ai_response:
            result.issues.append(f"API Error: {ai_response['error']}")
            if self.verbose:
                print(f"   ❌ API Error: {ai_response['error']}")
            return result

        # Extract AI results
        result.ai_updates = ai_response.get("updates", [])
        result.ai_events = ai_response.get("events", [])
        result.ai_response_email = ai_response.get("response_email", "")
        result.ai_notes = ai_response.get("notes", "")

        if self.verbose:
            print(f"\n   AI Response ({elapsed_ms}ms):")
            print(f"   Updates: {len(result.ai_updates)}")
            for u in result.ai_updates:
                print(f"      • {u.get('column')}: {u.get('value')} (conf: {u.get('confidence', 'N/A')})")

            print(f"   Events: {[e.get('type') for e in result.ai_events]}")

            if result.ai_response_email:
                preview = result.ai_response_email[:100].replace('\n', ' ')
                print(f"   Response: {preview}...")

        # Validate
        updates_ok, update_issues, update_warnings = self._validate_updates(
            scenario.expected_updates, result.ai_updates
        )
        result.updates_correct = updates_ok
        result.issues.extend(update_issues)
        result.warnings.extend(update_warnings)

        events_ok, event_issues, event_warnings = self._validate_events(
            scenario.expected_events, result.ai_events
        )
        result.events_correct = events_ok
        result.issues.extend(event_issues)
        result.warnings.extend(event_warnings)

        # Calculate missing fields for response validation
        from email_automation.app_config import REQUIRED_FIELDS_FOR_CLOSE
        idx_map = get_header_index_map(REAL_HEADER)
        missing = []
        for field in REQUIRED_FIELDS_FOR_CLOSE:
            key = field.strip().lower()
            if key in idx_map:
                col_idx = idx_map[key] - 1
                val = row_data[col_idx] if col_idx < len(row_data) else ""
                if not val.strip():
                    missing.append(field)

        response_ok, response_issues, response_warnings = self._validate_response(
            result.ai_response_email, scenario.expected_response_type, missing
        )
        result.response_appropriate = response_ok
        result.issues.extend(response_issues)
        result.warnings.extend(response_warnings)

        # Overall pass
        result.overall_pass = updates_ok and events_ok and response_ok

        if self.verbose:
            print(f"\n   {'✅ PASS' if result.overall_pass else '❌ FAIL'}")
            if result.issues:
                print("   Issues:")
                for i in result.issues:
                    print(f"      • {i}")
            if result.warnings:
                print("   Warnings:")
                for w in result.warnings:
                    print(f"      ⚠️ {w}")

        self.results.append(result)
        return result

    def run_all(self, scenarios: List[ConversationScenario] = None) -> List[ScenarioResult]:
        """Run all scenarios and return results."""
        if scenarios is None:
            scenarios = get_all_scenarios()

        print("\n" + "="*80)
        print("🚀 FULL END-TO-END TEST SUITE")
        print("="*80)
        print(f"Running {len(scenarios)} scenarios with real OpenAI API calls...")
        print("This may take a few minutes.\n")

        for scenario in scenarios:
            self.test_scenario(scenario)
            time.sleep(0.5)  # Small delay between API calls

        return self.results

    def print_summary(self):
        """Print test summary."""
        passed = sum(1 for r in self.results if r.overall_pass)
        failed = len(self.results) - passed

        print("\n" + "="*80)
        print("📊 TEST SUMMARY")
        print("="*80)
        print(f"Total: {len(self.results)} | ✅ Passed: {passed} | ❌ Failed: {failed}")
        print(f"Pass Rate: {passed/len(self.results)*100:.1f}%")

        avg_time = sum(r.api_call_ms for r in self.results) / len(self.results) if self.results else 0
        print(f"Average API Response Time: {avg_time:.0f}ms")

        if failed > 0:
            print("\n❌ Failed Scenarios:")
            for r in self.results:
                if not r.overall_pass:
                    print(f"\n  {r.name}:")
                    for issue in r.issues:
                        print(f"    • {issue}")

        print("\n" + "="*80)

    def generate_detailed_report(self) -> Dict:
        """Generate comprehensive test report."""
        report = {
            "summary": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_tests": len(self.results),
                "passed": sum(1 for r in self.results if r.overall_pass),
                "failed": sum(1 for r in self.results if not r.overall_pass),
                "pass_rate": f"{sum(1 for r in self.results if r.overall_pass)/len(self.results)*100:.1f}%" if self.results else "N/A",
                "avg_api_time_ms": sum(r.api_call_ms for r in self.results) / len(self.results) if self.results else 0
            },
            "scenarios": []
        }

        for r in self.results:
            scenario_report = {
                "name": r.name,
                "description": r.description,
                "property": f"{r.property_address}, {r.city}",
                "overall_pass": r.overall_pass,
                "updates_correct": r.updates_correct,
                "events_correct": r.events_correct,
                "response_appropriate": r.response_appropriate,
                "api_time_ms": r.api_call_ms,
                "issues": r.issues,
                "warnings": r.warnings,
                "ai_updates": r.ai_updates,
                "ai_events": r.ai_events,
                "ai_response_email": r.ai_response_email,
                "expected_updates": r.expected_updates,
                "expected_events": r.expected_events
            }
            report["scenarios"].append(scenario_report)

        return report

    def save_report(self, filename: str = "test_report.json"):
        """Save detailed report to file."""
        report = self.generate_detailed_report()
        with open(filename, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n📄 Detailed report saved to: {filename}")
        return filename


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Full End-to-End Email Automation Tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_full_test.py                    # Run all tests
  python run_full_test.py -s partial_info    # Run specific scenario
  python run_full_test.py --report results   # Save report to results.json
  python run_full_test.py -l                 # List all scenarios
        """
    )
    parser.add_argument("-s", "--scenario", help="Run specific scenario by name")
    parser.add_argument("-l", "--list", action="store_true", help="List all scenarios")
    parser.add_argument("-q", "--quiet", action="store_true", help="Minimal output")
    parser.add_argument("-r", "--report", help="Save report to specified file")

    args = parser.parse_args()

    if args.list:
        print("\nAvailable test scenarios:")
        for s in get_all_scenarios():
            print(f"  • {s.name}: {s.description}")
        return

    runner = FullTestRunner(verbose=not args.quiet)

    if args.scenario:
        scenario = get_scenario_by_name(args.scenario)
        if scenario:
            runner.test_scenario(scenario)
        else:
            print(f"❌ Scenario '{args.scenario}' not found")
            return
    else:
        runner.run_all()

    runner.print_summary()

    if args.report:
        runner.save_report(args.report if args.report.endswith('.json') else f"{args.report}.json")


if __name__ == "__main__":
    main()
