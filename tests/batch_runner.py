#!/usr/bin/env python3
"""
Batch Test Runner
=================
Runs hundreds of test cases with parallel execution and detailed result collection.

Usage:
    python tests/batch_runner.py --suite tests/generated_suite/ --output tests/results/run_001/
    python tests/batch_runner.py --suite tests/generated_suite/ --parallel 4
    python tests/batch_runner.py --suite tests/generated_suite/ --category escalations
"""

import os
import sys
import json
import time
import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ============================================================================
# ENVIRONMENT SETUP
# ============================================================================

env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                if key not in os.environ:
                    os.environ[key] = value

if not os.getenv("OPENAI_API_KEY"):
    print("ERROR: OPENAI_API_KEY not set")
    sys.exit(1)

for var in ["AZURE_API_APP_ID", "AZURE_API_CLIENT_SECRET", "FIREBASE_API_KEY"]:
    if not os.environ.get(var):
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
# DATA STRUCTURES
# ============================================================================

SHEET_HEADER = [
    "Property Address", "City", "Property Name", "Leasing Company",
    "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr", "Ops Ex /SF",
    "Gross Rent", "Drive Ins", "Docks", "Ceiling Ht", "Power",
    "Listing Brokers Comments", "Flyer / Link", "Floorplan",
    "Jill and Clients comments"
]

REQUIRED_FIELDS = ["total sf", "ops ex /sf", "drive ins", "docks", "ceiling ht", "power"]


@dataclass
class TestResult:
    """Result of a single test."""
    test_id: str
    category: str
    type: str
    passed: bool
    duration_ms: int

    # AI outputs
    updates: List[Dict] = field(default_factory=list)
    events: List[Dict] = field(default_factory=list)
    response_email: Optional[str] = None
    notes: str = ""

    # Validation
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # For debugging
    property_address: str = ""
    conversation_preview: str = ""


@dataclass
class BatchResults:
    """Results from a batch run."""
    run_id: str
    started_at: str
    completed_at: str = ""
    duration_s: float = 0

    total_tests: int = 0
    passed: int = 0
    failed: int = 0

    results: List[TestResult] = field(default_factory=list)
    by_category: Dict[str, Dict] = field(default_factory=dict)
    failures: List[Dict] = field(default_factory=list)

    # Performance
    latencies: List[int] = field(default_factory=list)
    avg_latency_ms: float = 0
    p50_latency_ms: float = 0
    p90_latency_ms: float = 0
    p99_latency_ms: float = 0


# ============================================================================
# TEST EXECUTION
# ============================================================================

class BatchRunner:
    """Runs test cases and collects results."""

    def __init__(self, suite_path: str, output_path: str = None):
        self.suite_path = Path(suite_path)
        self.output_path = Path(output_path) if output_path else None
        self.results = BatchResults(
            run_id=f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            started_at=datetime.now(timezone.utc).isoformat()
        )
        self.lock = threading.Lock()
        self.progress_count = 0
        self.total_count = 0

    def load_test_cases(self, category: str = None) -> List[Dict]:
        """Load test cases from suite directory."""
        test_cases = []

        # Load from category directories
        for cat_dir in self.suite_path.iterdir():
            if not cat_dir.is_dir():
                continue

            if cat_dir.name in ["manifest.json", "properties.json"]:
                continue

            if category and cat_dir.name != category:
                continue

            for test_file in cat_dir.glob("*.json"):
                with open(test_file) as f:
                    test_case = json.load(f)
                    test_case["_file"] = str(test_file)
                    test_cases.append(test_case)

        return test_cases

    def build_rowvals(self, prop: Dict) -> List[str]:
        """Build initial row values from property."""
        row = [""] * len(SHEET_HEADER)
        row[0] = prop.get("address", "")
        row[1] = prop.get("city", "")
        row[4] = prop.get("contact", "")
        row[5] = prop.get("email", "")
        return row

    def build_conversation_payload(self, prop: Dict, conversation: List[Dict]) -> List[Dict]:
        """Build conversation payload for AI."""
        payload = []
        for i, msg in enumerate(conversation):
            payload.append({
                "direction": msg["direction"],
                "from": prop["email"] if msg["direction"] == "inbound" else "jill@company.com",
                "to": ["jill@company.com"] if msg["direction"] == "inbound" else [prop["email"]],
                "subject": f"{prop['address']}, {prop.get('city', '')}",
                "timestamp": f"2024-01-15T{10+i}:00:00Z",
                "preview": msg["content"][:200],
                "content": msg["content"]
            })
        return payload

    def run_single_test(self, test_case: Dict) -> TestResult:
        """Run a single test case."""
        test_id = test_case["id"]
        category = test_case["category"]
        test_type = test_case["type"]
        prop = test_case["property"]
        conversation = test_case["conversation"]
        expected = test_case.get("expected", {})
        forbidden = test_case.get("forbidden", {})

        start_time = time.time()

        result = TestResult(
            test_id=test_id,
            category=category,
            type=test_type,
            passed=False,
            duration_ms=0,
            property_address=prop.get("address", ""),
            conversation_preview=conversation[-1]["content"][:100] if conversation else ""
        )

        try:
            # Build inputs
            rowvals = self.build_rowvals(prop)
            conv_payload = self.build_conversation_payload(prop, conversation)

            # Call AI
            proposal = propose_sheet_updates(
                uid="batch-test-user",
                client_id="batch-test-client",
                email=prop["email"],
                sheet_id="batch-test-sheet",
                header=SHEET_HEADER,
                rownum=prop.get("rowIndex", 3),
                rowvals=rowvals,
                thread_id=f"thread-{test_id}",
                contact_name=prop.get("contact", ""),
                conversation=conv_payload,
                dry_run=True
            )

            if proposal:
                result.updates = proposal.get("updates", [])
                result.events = proposal.get("events", [])
                result.response_email = proposal.get("response_email")
                result.notes = proposal.get("notes", "")

            # Validate results
            result.issues, result.warnings = self.validate_result(
                result, expected, forbidden
            )

            result.passed = len(result.issues) == 0

        except Exception as e:
            result.issues.append(f"Exception: {str(e)}")
            result.passed = False

        result.duration_ms = int((time.time() - start_time) * 1000)

        # Update progress
        with self.lock:
            self.progress_count += 1
            if self.progress_count % 10 == 0 or self.progress_count == self.total_count:
                pct = (self.progress_count / self.total_count) * 100
                print(f"  Progress: {self.progress_count}/{self.total_count} ({pct:.1f}%)")

        return result

    def validate_result(
        self,
        result: TestResult,
        expected: Dict,
        forbidden: Dict
    ) -> Tuple[List[str], List[str]]:
        """Validate test result against expectations."""
        issues = []
        warnings = []

        # Check forbidden updates
        actual_update_cols = {u.get("column", "").lower() for u in result.updates}
        for forbidden_col in forbidden.get("updates", []):
            if forbidden_col.lower() in actual_update_cols:
                issues.append(f"FORBIDDEN update: {forbidden_col}")

        # Check forbidden requests in response email
        if result.response_email:
            response_lower = result.response_email.lower()
            for forbidden_req in forbidden.get("requests", []):
                if forbidden_req.lower() in response_lower:
                    issues.append(f"FORBIDDEN request in email: {forbidden_req}")

        # Check expected events
        expected_events = expected.get("events", [])
        actual_event_types = {e.get("type") for e in result.events}

        for exp_event in expected_events:
            exp_type = exp_event.get("type") if isinstance(exp_event, dict) else exp_event
            if exp_type not in actual_event_types:
                issues.append(f"Missing expected event: {exp_type}")

        # Check expected response email behavior
        if "response_email" in expected:
            exp_email = expected["response_email"]
            if exp_email is None and result.response_email:
                issues.append("Expected no response email but got one (should escalate)")
            elif exp_email == "ask_for_phone":
                # Should have response email asking for phone number
                if not result.response_email:
                    issues.append("Expected response email asking for phone number but got none")
                elif "phone" not in result.response_email.lower() and "number" not in result.response_email.lower() and "call" not in result.response_email.lower():
                    warnings.append("Response email may not be asking for phone number")

        # Check row_complete expectation
        if expected.get("row_complete"):
            # Verify all required fields were updated
            updated_fields = {u.get("column", "").lower() for u in result.updates}
            for req in REQUIRED_FIELDS:
                if req not in updated_fields:
                    warnings.append(f"Expected row complete but missing: {req}")

        return issues, warnings

    def run_batch(self, test_cases: List[Dict], parallel: int = 1) -> BatchResults:
        """Run all test cases."""
        self.total_count = len(test_cases)
        self.progress_count = 0

        print(f"\nRunning {len(test_cases)} tests with {parallel} worker(s)...")

        if parallel > 1:
            with ThreadPoolExecutor(max_workers=parallel) as executor:
                futures = {executor.submit(self.run_single_test, tc): tc for tc in test_cases}
                for future in as_completed(futures):
                    result = future.result()
                    self.results.results.append(result)
        else:
            for tc in test_cases:
                result = self.run_single_test(tc)
                self.results.results.append(result)

        # Calculate statistics
        self.results.completed_at = datetime.now(timezone.utc).isoformat()
        self.results.total_tests = len(self.results.results)
        self.results.passed = sum(1 for r in self.results.results if r.passed)
        self.results.failed = self.results.total_tests - self.results.passed

        # Latency stats
        latencies = sorted([r.duration_ms for r in self.results.results])
        self.results.latencies = latencies
        if latencies:
            self.results.avg_latency_ms = sum(latencies) / len(latencies)
            self.results.p50_latency_ms = latencies[len(latencies) // 2]
            self.results.p90_latency_ms = latencies[int(len(latencies) * 0.9)]
            self.results.p99_latency_ms = latencies[int(len(latencies) * 0.99)]

        # By category
        categories = {}
        for r in self.results.results:
            if r.category not in categories:
                categories[r.category] = {"total": 0, "passed": 0, "failed": 0}
            categories[r.category]["total"] += 1
            if r.passed:
                categories[r.category]["passed"] += 1
            else:
                categories[r.category]["failed"] += 1
        self.results.by_category = categories

        # Collect failures
        self.results.failures = [
            {
                "test_id": r.test_id,
                "category": r.category,
                "type": r.type,
                "issues": r.issues,
                "property": r.property_address,
                "conversation_preview": r.conversation_preview
            }
            for r in self.results.results if not r.passed
        ]

        return self.results

    def save_results(self):
        """Save results to output directory."""
        if not self.output_path:
            return

        self.output_path.mkdir(parents=True, exist_ok=True)

        # Save summary
        summary = {
            "run_id": self.results.run_id,
            "started_at": self.results.started_at,
            "completed_at": self.results.completed_at,
            "total_tests": self.results.total_tests,
            "passed": self.results.passed,
            "failed": self.results.failed,
            "pass_rate": (self.results.passed / self.results.total_tests * 100) if self.results.total_tests else 0,
            "avg_latency_ms": self.results.avg_latency_ms,
            "p50_latency_ms": self.results.p50_latency_ms,
            "p90_latency_ms": self.results.p90_latency_ms,
            "p99_latency_ms": self.results.p99_latency_ms,
            "by_category": self.results.by_category
        }

        with open(self.output_path / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)

        # Save failures
        with open(self.output_path / "failures.json", "w") as f:
            json.dump(self.results.failures, f, indent=2)

        # Save all results
        all_results = [asdict(r) for r in self.results.results]
        with open(self.output_path / "all_results.json", "w") as f:
            json.dump(all_results, f, indent=2)

        print(f"\nResults saved to: {self.output_path}")

    def print_summary(self):
        """Print summary to console."""
        print(f"\n{'='*60}")
        print("BATCH TEST RESULTS")
        print(f"{'='*60}")
        print(f"Run ID: {self.results.run_id}")
        print(f"Total: {self.results.total_tests} | Passed: {self.results.passed} | Failed: {self.results.failed}")

        if self.results.total_tests:
            rate = (self.results.passed / self.results.total_tests) * 100
            print(f"Pass Rate: {rate:.1f}%")

        print(f"\nLatency: avg={self.results.avg_latency_ms:.0f}ms, p50={self.results.p50_latency_ms:.0f}ms, p90={self.results.p90_latency_ms:.0f}ms, p99={self.results.p99_latency_ms:.0f}ms")

        print(f"\nBy Category:")
        for cat, stats in self.results.by_category.items():
            rate = (stats["passed"] / stats["total"] * 100) if stats["total"] else 0
            print(f"  {cat}: {stats['passed']}/{stats['total']} ({rate:.1f}%)")

        if self.results.failures:
            print(f"\nFailed Tests ({len(self.results.failures)}):")
            for f in self.results.failures[:10]:  # Show first 10
                print(f"  - {f['test_id']}")
                for issue in f["issues"][:2]:
                    print(f"      {issue}")
            if len(self.results.failures) > 10:
                print(f"  ... and {len(self.results.failures) - 10} more")


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Batch Test Runner")
    parser.add_argument("--suite", "-s", required=True, help="Test suite directory")
    parser.add_argument("--output", "-o", help="Output directory for results")
    parser.add_argument("--parallel", "-p", type=int, default=1, help="Parallel workers")
    parser.add_argument("--category", "-c", help="Run only specific category")
    parser.add_argument("--limit", "-l", type=int, help="Limit number of tests")
    args = parser.parse_args()

    runner = BatchRunner(args.suite, args.output)

    # Load test cases
    print(f"Loading tests from: {args.suite}")
    test_cases = runner.load_test_cases(args.category)

    if not test_cases:
        print("No test cases found!")
        sys.exit(1)

    if args.limit:
        test_cases = test_cases[:args.limit]

    print(f"Found {len(test_cases)} test cases")

    # Run tests
    runner.run_batch(test_cases, parallel=args.parallel)

    # Print and save results
    runner.print_summary()

    if args.output:
        runner.save_results()


if __name__ == "__main__":
    main()
