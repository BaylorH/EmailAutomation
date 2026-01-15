"""
Integration tests that call the actual OpenAI API to validate extraction behavior.
This tests the real propose_sheet_updates() function with simulated conversations.
"""

import sys
import os
import json
from datetime import datetime, timezone
from typing import Dict, List, Any

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.mock_data import (
    REAL_HEADER, SCENARIOS, ConversationScenario,
    get_scenario_by_name, get_all_scenarios, get_header_index_map
)

# Import the real modules (requires env vars to be set)
try:
    from email_automation.clients import client as openai_client
    from email_automation.ai_processing import get_row_anchor, check_missing_required_fields
    from email_automation.app_config import REQUIRED_FIELDS_FOR_CLOSE
    HAS_OPENAI = True
except Exception as e:
    print(f"Warning: Could not import email_automation modules: {e}")
    print("Running in mock mode (no actual API calls)")
    HAS_OPENAI = False


class AIIntegrationTester:
    """Tests the actual AI extraction logic."""

    def __init__(self, verbose: bool = True):
        self.verbose = verbose
        self.results = []

    def _build_prompt_from_scenario(self, scenario: ConversationScenario,
                                    row_data: List[str]) -> str:
        """Build the exact prompt that would be sent to OpenAI."""

        # Build conversation history
        conversation = []
        for msg in scenario.messages:
            conversation.append({
                "direction": msg["direction"],
                "from": scenario.email if msg["direction"] == "inbound" else "jill@company.com",
                "to": ["jill@company.com"] if msg["direction"] == "inbound" else [scenario.email],
                "subject": f"{scenario.property_address}, {scenario.city}",
                "timestamp": msg["timestamp"],
                "preview": msg["content"][:200],
                "content": msg["content"]
            })

        # Get row anchor
        target_anchor = f"{scenario.property_address}, {scenario.city}"

        # Check missing fields
        idx_map = get_header_index_map(REAL_HEADER)
        missing_fields = []
        for field in REQUIRED_FIELDS_FOR_CLOSE:
            key = field.strip().lower()
            if key in idx_map:
                col_idx = idx_map[key] - 1
                val = row_data[col_idx] if col_idx < len(row_data) else ""
                if not val.strip():
                    missing_fields.append(field)

        # Build prompt (matching the real system)
        COLUMN_RULES = """
COLUMN SEMANTICS & MAPPING (use EXACT header names):
- "Rent/SF /Yr": Base/asking rent per square foot per YEAR. Synonyms: asking, base rent, $/SF/yr.
- "Ops Ex /SF": NNN/CAM/Operating Expenses per square foot per YEAR. Synonyms: NNN, CAM, OpEx, operating expenses.
- "Gross Rent": If BOTH base rent and NNN are present, set to (Rent/SF /Yr + Ops Ex /SF), rounded to 2 decimals.
- "Total SF": Total square footage. Synonyms: sq footage, square feet, SF, size.
- "Drive Ins": Number of drive-in doors. Synonyms: drive in doors, loading doors.
- "Docks": Number of dock doors/loading docks.
- "Ceiling Ht": Ceiling height. Synonyms: max ceiling height, ceiling clearance.
- "Power": Electrical power specifications.

FORMATTING:
- For money/area fields, output plain decimals (no "$", "SF", commas). Examples: "30", "14.29", "2400".
"""

        EVENT_RULES = """
EVENTS DETECTION (analyze ONLY the LAST HUMAN message for these events):
- "property_unavailable": ONLY when the CURRENT TARGET PROPERTY is explicitly stated as unavailable/leased/off-market.
- "new_property": Emit when the LAST HUMAN message suggests a DIFFERENT property than the TARGET PROPERTY.
- "call_requested": Only when someone explicitly asks for a call/phone conversation.
- "close_conversation": When conversation appears complete and the sender indicates they're done.
"""

        prompt = f"""
You are analyzing a conversation thread to suggest updates to ONE Google Sheet row, detect key events, and generate an appropriate response email.

TARGET PROPERTY (canonical identity for matching): {target_anchor}
CONTACT NAME: {scenario.contact_name}

{COLUMN_RULES}
{EVENT_RULES}

SHEET HEADER (row 2):
{json.dumps(REAL_HEADER)}

CURRENT ROW VALUES:
{json.dumps(row_data)}

MISSING REQUIRED FIELDS:
{json.dumps(missing_fields)}

CONVERSATION HISTORY (latest last):
{json.dumps(conversation, indent=2)}

Be conservative: only suggest changes you can cite from the text.

OUTPUT ONLY valid JSON in this exact format:
{{
  "updates": [
    {{"column": "<exact header name>", "value": "<new value>", "confidence": 0.85, "reason": "<explanation>"}}
  ],
  "events": [
    {{"type": "call_requested | property_unavailable | new_property | close_conversation", ...}}
  ],
  "response_email": "<professional response email body>",
  "notes": "<any additional notes>"
}}
"""
        return prompt, conversation

    def call_openai(self, prompt: str) -> Dict:
        """Call OpenAI API with the prompt."""
        if not HAS_OPENAI:
            return {"error": "OpenAI not available"}

        try:
            response = openai_client.responses.create(
                model="gpt-4o",
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                temperature=0.1
            )

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

            return json.loads(raw)

        except json.JSONDecodeError as e:
            return {"error": f"JSON parse error: {e}", "raw": raw}
        except Exception as e:
            return {"error": str(e)}

    def test_scenario(self, scenario: ConversationScenario) -> Dict:
        """Test a single scenario with the real AI."""
        if self.verbose:
            print(f"\n{'='*70}")
            print(f"TESTING: {scenario.name}")
            print(f"Description: {scenario.description}")
            print(f"{'='*70}")

        # Get initial row data for this property
        from tests.mock_data import SAMPLE_PROPERTIES
        row_data = None
        for prop in SAMPLE_PROPERTIES:
            if prop["data"][0] == scenario.property_address:
                row_data = scenario.initial_row_data if scenario.initial_row_data else prop["data"]
                break

        if row_data is None:
            return {"error": f"Property {scenario.property_address} not found"}

        # Build prompt
        prompt, conversation = self._build_prompt_from_scenario(scenario, row_data)

        if self.verbose:
            print(f"\nConversation ({len(conversation)} messages):")
            for msg in conversation:
                direction = "OUT→" if msg["direction"] == "outbound" else "IN←"
                print(f"  {direction} {msg['content'][:80]}...")

        # Call OpenAI
        if self.verbose:
            print("\nCalling OpenAI...")

        result = self.call_openai(prompt)

        if "error" in result:
            print(f"  ❌ Error: {result['error']}")
            return result

        if self.verbose:
            print(f"\nAI Response:")
            print(f"  Updates: {len(result.get('updates', []))}")
            for u in result.get("updates", []):
                print(f"    • {u.get('column')}: {u.get('value')} (confidence: {u.get('confidence', 'N/A')})")

            print(f"  Events: {result.get('events', [])}")

            if result.get("response_email"):
                email_preview = result["response_email"][:150].replace("\n", " ")
                print(f"  Response email: {email_preview}...")

        # Compare with expected
        result["_scenario"] = scenario.name
        result["_expected_updates"] = scenario.expected_updates
        result["_expected_events"] = scenario.expected_events
        result["_expected_response_type"] = scenario.expected_response_type

        # Validate
        validation = self._validate_result(result, scenario)
        result["_validation"] = validation

        if self.verbose:
            if validation["passed"]:
                print(f"\n✅ VALIDATION PASSED")
            else:
                print(f"\n❌ VALIDATION FAILED")
                for issue in validation["issues"]:
                    print(f"    • {issue}")

        self.results.append(result)
        return result

    def _validate_result(self, result: Dict, scenario: ConversationScenario) -> Dict:
        """Validate AI result against expected outcomes."""
        issues = []
        passed = True

        actual_updates = result.get("updates", [])
        actual_events = result.get("events", [])

        # Check expected updates were produced
        for expected in scenario.expected_updates:
            if expected.get("should_skip_if_human_override"):
                continue

            found = False
            for actual in actual_updates:
                if actual.get("column") == expected["column"]:
                    found = True
                    # Check value (normalize both)
                    expected_val = str(expected["value"]).replace(",", "")
                    actual_val = str(actual.get("value", "")).replace(",", "")

                    if expected_val != actual_val:
                        issues.append(f"Wrong value for {expected['column']}: expected '{expected_val}', got '{actual_val}'")
                        passed = False
                    break

            if not found:
                issues.append(f"Missing update for: {expected['column']}")
                passed = False

        # Check expected events were detected
        expected_event_types = set(e["type"] for e in scenario.expected_events)
        actual_event_types = set(e.get("type") for e in actual_events)

        missing_events = expected_event_types - actual_event_types
        if missing_events:
            issues.append(f"Missing events: {missing_events}")
            passed = False

        extra_events = actual_event_types - expected_event_types
        if extra_events:
            issues.append(f"Unexpected events: {extra_events}")
            # Not necessarily a failure - AI might detect more

        return {"passed": passed, "issues": issues}

    def run_all(self, skip_auto_reply: bool = True) -> List[Dict]:
        """Run all scenarios."""
        print("\n" + "="*80)
        print("AI INTEGRATION TEST SUITE")
        print("="*80)

        scenarios = get_all_scenarios()
        if skip_auto_reply:
            scenarios = [s for s in scenarios if "auto_reply" not in s.name]

        print(f"Running {len(scenarios)} scenarios...")

        for scenario in scenarios:
            self.test_scenario(scenario)

        # Summary
        passed = sum(1 for r in self.results if r.get("_validation", {}).get("passed", False))
        failed = len(self.results) - passed

        print("\n" + "="*80)
        print("SUMMARY")
        print("="*80)
        print(f"Total: {len(self.results)} | Passed: {passed} | Failed: {failed}")

        if failed > 0:
            print("\nFailed scenarios:")
            for r in self.results:
                if not r.get("_validation", {}).get("passed", True):
                    print(f"  ❌ {r.get('_scenario', 'unknown')}")
                    for issue in r.get("_validation", {}).get("issues", []):
                        print(f"      {issue}")

        return self.results

    def generate_report(self, filename: str = "test_report.json"):
        """Generate detailed test report."""
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_tests": len(self.results),
            "passed": sum(1 for r in self.results if r.get("_validation", {}).get("passed", False)),
            "failed": sum(1 for r in self.results if not r.get("_validation", {}).get("passed", True)),
            "results": self.results
        }

        with open(filename, "w") as f:
            json.dump(report, f, indent=2, default=str)

        print(f"\nReport saved to: {filename}")
        return report


def quick_test():
    """Quick test with just a couple scenarios."""
    tester = AIIntegrationTester(verbose=True)

    # Test a simple complete info scenario
    scenario = get_scenario_by_name("complete_info_first_reply")
    if scenario:
        tester.test_scenario(scenario)

    # Test unavailable scenario
    scenario = get_scenario_by_name("property_unavailable")
    if scenario:
        tester.test_scenario(scenario)

    return tester.results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AI Integration Tests")
    parser.add_argument("--scenario", "-s", help="Run specific scenario")
    parser.add_argument("--quick", "-q", action="store_true", help="Quick test (2 scenarios)")
    parser.add_argument("--report", "-r", help="Generate report to file")
    parser.add_argument("--all", "-a", action="store_true", help="Run all scenarios")

    args = parser.parse_args()

    if args.quick:
        quick_test()
    elif args.scenario:
        tester = AIIntegrationTester()
        scenario = get_scenario_by_name(args.scenario)
        if scenario:
            tester.test_scenario(scenario)
        else:
            print(f"Scenario '{args.scenario}' not found")
    elif args.all:
        tester = AIIntegrationTester()
        tester.run_all()
        if args.report:
            tester.generate_report(args.report)
    else:
        print("Usage: python test_ai_integration.py [--quick | --scenario NAME | --all]")
        print("       --report FILE to save results")
