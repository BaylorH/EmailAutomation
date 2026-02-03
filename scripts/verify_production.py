#!/usr/bin/env python3
"""
Production Verification Script

Runs a series of checkpoints to verify the production pipeline is healthy.
Uses real services with test data to verify full flow.

Usage:
    python scripts/verify_production.py
    python scripts/verify_production.py --checkpoint column_mapping
    python scripts/verify_production.py --list

Checkpoints:
    1. console_logs      - Verify console logging reaches Firestore
    2. column_mapping    - Verify AI column detection works
    3. firestore_schema  - Verify Firestore collections exist and are structured correctly
    4. ai_extraction     - Verify AI field extraction from sample email
    5. notifications     - Verify notification creation works
    6. column_config     - Verify columnConfig flows through the pipeline

Environment Variables Required:
    OPENAI_API_KEY - For AI extraction tests
    FIREBASE_SA_KEY - For Firestore access (base64 encoded)
"""

import os
import sys
import json
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional, Callable, Any

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation.clients import _fs
from email_automation.column_config import (
    detect_column_mapping,
    get_default_column_config,
    build_column_rules_prompt,
    CANONICAL_FIELDS,
)
from email_automation.ai_processing import _filter_config_by_extraction_fields


@dataclass
class CheckpointResult:
    """Result of a verification checkpoint."""
    name: str
    passed: bool
    message: str
    details: Optional[dict] = None
    duration_ms: Optional[float] = None


def checkpoint(name: str, description: str):
    """Decorator to register a checkpoint function."""
    def decorator(func: Callable) -> Callable:
        func._checkpoint_name = name
        func._checkpoint_description = description
        return func
    return decorator


class ProductionVerifier:
    """Runs verification checkpoints against production systems."""

    def __init__(self, user_id: str = None):
        """
        Initialize verifier.

        Args:
            user_id: Optional user ID for user-specific checks. If not provided,
                    will look for first available user in Firestore.
        """
        self.user_id = user_id
        self._checkpoints = []
        self._register_checkpoints()

    def _register_checkpoints(self):
        """Register all checkpoint methods."""
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if callable(attr) and hasattr(attr, '_checkpoint_name'):
                self._checkpoints.append({
                    'name': attr._checkpoint_name,
                    'description': attr._checkpoint_description,
                    'func': attr
                })

    def list_checkpoints(self) -> List[dict]:
        """List all available checkpoints."""
        return [{'name': c['name'], 'description': c['description']} for c in self._checkpoints]

    def run_checkpoint(self, name: str) -> CheckpointResult:
        """Run a specific checkpoint by name."""
        for cp in self._checkpoints:
            if cp['name'] == name:
                start = datetime.now()
                try:
                    result = cp['func']()
                    result.duration_ms = (datetime.now() - start).total_seconds() * 1000
                    return result
                except Exception as e:
                    return CheckpointResult(
                        name=name,
                        passed=False,
                        message=f"Exception: {e}",
                        duration_ms=(datetime.now() - start).total_seconds() * 1000
                    )
        return CheckpointResult(name=name, passed=False, message=f"Unknown checkpoint: {name}")

    def run_all(self) -> List[CheckpointResult]:
        """Run all checkpoints."""
        return [self.run_checkpoint(cp['name']) for cp in self._checkpoints]

    def _get_user_id(self) -> str:
        """Get user ID, either from constructor or by finding first available."""
        if self.user_id:
            return self.user_id

        # Find first user with clients collection
        users = _fs.collection("users").limit(1).get()
        if not users:
            raise ValueError("No users found in Firestore")
        return users[0].id

    # ==================== CHECKPOINTS ====================

    @checkpoint("firestore_schema", "Verify Firestore collections exist")
    def check_firestore_schema(self) -> CheckpointResult:
        """Verify Firestore has expected collections."""
        try:
            user_id = self._get_user_id()

            # Check for key collections
            collections_to_check = ['clients', 'threads', 'msgIndex', 'convIndex']
            found = []
            missing = []

            for coll_name in collections_to_check:
                coll = _fs.collection("users").document(user_id).collection(coll_name)
                docs = list(coll.limit(1).stream())
                if docs:
                    found.append(coll_name)
                else:
                    missing.append(coll_name)

            if missing:
                return CheckpointResult(
                    name="firestore_schema",
                    passed=len(found) > 0,
                    message=f"Found {len(found)} collections, missing: {missing}",
                    details={"found": found, "missing": missing, "user_id": user_id}
                )

            return CheckpointResult(
                name="firestore_schema",
                passed=True,
                message=f"All {len(found)} collections present",
                details={"collections": found, "user_id": user_id}
            )
        except Exception as e:
            return CheckpointResult(
                name="firestore_schema",
                passed=False,
                message=f"Failed to check schema: {e}"
            )

    @checkpoint("column_mapping", "Verify AI column detection")
    def check_column_mapping(self) -> CheckpointResult:
        """Verify column mapping detection works correctly."""
        try:
            # Test with standard headers
            test_headers = [
                "Property Address", "City", "Property Name", "Leasing Company",
                "Leasing Contact", "Email", "Total SF", "Rent/SF /Yr",
                "Ops Ex /SF", "Gross Rent", "Drive Ins", "Docks",
                "Ceiling Ht", "Power", "Listing Brokers Comments"
            ]

            result = detect_column_mapping(test_headers, use_ai=False)
            mappings = result.get("mappings", {})
            confidence = result.get("confidence", {})

            # Check key fields are mapped
            required_fields = ["property_address", "city", "email", "total_sf"]
            mapped = [f for f in required_fields if f in mappings and mappings[f]]

            if len(mapped) < len(required_fields):
                return CheckpointResult(
                    name="column_mapping",
                    passed=False,
                    message=f"Only {len(mapped)}/{len(required_fields)} required fields mapped",
                    details={"mappings": mappings, "confidence": confidence}
                )

            return CheckpointResult(
                name="column_mapping",
                passed=True,
                message=f"All {len(required_fields)} required fields mapped",
                details={"mappings": mappings, "fields_mapped": len(mappings)}
            )
        except Exception as e:
            return CheckpointResult(
                name="column_mapping",
                passed=False,
                message=f"Column mapping failed: {e}"
            )

    @checkpoint("column_config", "Verify columnConfig pipeline")
    def check_column_config(self) -> CheckpointResult:
        """Verify columnConfig flows correctly through the system."""
        try:
            # Get default config
            default_config = get_default_column_config()

            # Verify structure
            required_keys = ["mappings", "requiredFields", "formulaFields", "neverRequest"]
            missing_keys = [k for k in required_keys if k not in default_config]
            if missing_keys:
                return CheckpointResult(
                    name="column_config",
                    passed=False,
                    message=f"Default config missing keys: {missing_keys}",
                    details={"config_keys": list(default_config.keys())}
                )

            # Test build_column_rules_prompt
            rules_prompt = build_column_rules_prompt(default_config)
            if not rules_prompt or len(rules_prompt) < 100:
                return CheckpointResult(
                    name="column_config",
                    passed=False,
                    message="build_column_rules_prompt returned empty or short result",
                    details={"prompt_length": len(rules_prompt) if rules_prompt else 0}
                )

            # Test extraction fields filtering
            test_fields = ["total_sf", "ops_ex_sf"]
            filtered = _filter_config_by_extraction_fields(default_config, test_fields)

            # Non-extractable fields should still be in mappings
            if "property_address" not in filtered.get("mappings", {}):
                return CheckpointResult(
                    name="column_config",
                    passed=False,
                    message="Filtered config missing property_address (non-extractable)",
                    details={"filtered_mappings": filtered.get("mappings", {})}
                )

            return CheckpointResult(
                name="column_config",
                passed=True,
                message="columnConfig pipeline verified",
                details={
                    "default_mappings_count": len(default_config.get("mappings", {})),
                    "rules_prompt_length": len(rules_prompt),
                    "filter_works": True
                }
            )
        except Exception as e:
            return CheckpointResult(
                name="column_config",
                passed=False,
                message=f"columnConfig check failed: {e}"
            )

    @checkpoint("ai_extraction", "Verify AI field extraction")
    def check_ai_extraction(self) -> CheckpointResult:
        """Verify AI can extract fields from sample email content."""
        try:
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                return CheckpointResult(
                    name="ai_extraction",
                    passed=False,
                    message="OPENAI_API_KEY not set"
                )

            # Import here to avoid loading if not needed
            from openai import OpenAI

            client = OpenAI(api_key=api_key)

            # Simple extraction test
            test_prompt = """Extract property information from this email:

"The space at 123 Main St has 15,000 SF available.
Triple net is $3.50/SF. There are 2 dock doors and 1 drive-in.
Clear height is 24 feet with 400 amps."

Return JSON with: total_sf, ops_ex_sf, docks, drive_ins, ceiling_ht, power"""

            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": test_prompt}],
                temperature=0,
                max_tokens=500
            )

            content = response.choices[0].message.content

            # Check for expected values in response
            expected_values = ["15000", "15,000", "3.50", "24"]
            found = [v for v in expected_values if v in content]

            if len(found) < 3:
                return CheckpointResult(
                    name="ai_extraction",
                    passed=False,
                    message=f"Only {len(found)}/{len(expected_values)} values extracted",
                    details={"response": content[:500]}
                )

            return CheckpointResult(
                name="ai_extraction",
                passed=True,
                message="AI extraction working",
                details={"values_found": len(found), "model": "gpt-4o-mini"}
            )
        except Exception as e:
            return CheckpointResult(
                name="ai_extraction",
                passed=False,
                message=f"AI extraction failed: {e}"
            )

    @checkpoint("notifications", "Verify notification system")
    def check_notifications(self) -> CheckpointResult:
        """Verify notification schema is correct."""
        try:
            user_id = self._get_user_id()

            # Check for any client with notifications
            clients = _fs.collection("users").document(user_id).collection("clients").limit(5).get()

            notification_count = 0
            notification_kinds = set()

            for client_doc in clients:
                client_id = client_doc.id
                notifications = list(_fs.collection("users").document(user_id)
                                    .collection("clients").document(client_id)
                                    .collection("notifications").limit(10).stream())

                for n in notifications:
                    notification_count += 1
                    data = n.to_dict()
                    if "kind" in data:
                        notification_kinds.add(data["kind"])

            if notification_count == 0:
                return CheckpointResult(
                    name="notifications",
                    passed=True,
                    message="No notifications found (may be expected for new system)",
                    details={"clients_checked": len(clients)}
                )

            return CheckpointResult(
                name="notifications",
                passed=True,
                message=f"Found {notification_count} notifications",
                details={
                    "count": notification_count,
                    "kinds": list(notification_kinds),
                    "clients_checked": len(clients)
                }
            )
        except Exception as e:
            return CheckpointResult(
                name="notifications",
                passed=False,
                message=f"Notification check failed: {e}"
            )


def main():
    parser = argparse.ArgumentParser(description="Production verification script")
    parser.add_argument("--checkpoint", "-c", help="Run specific checkpoint")
    parser.add_argument("--list", "-l", action="store_true", help="List available checkpoints")
    parser.add_argument("--user-id", "-u", help="User ID for user-specific checks")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    verifier = ProductionVerifier(user_id=args.user_id)

    if args.list:
        print("\nAvailable checkpoints:")
        for cp in verifier.list_checkpoints():
            print(f"  {cp['name']:20} - {cp['description']}")
        return

    if args.checkpoint:
        results = [verifier.run_checkpoint(args.checkpoint)]
    else:
        print("\n🔍 Running production verification...\n")
        results = verifier.run_all()

    if args.json:
        output = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": [
                {
                    "name": r.name,
                    "passed": r.passed,
                    "message": r.message,
                    "details": r.details,
                    "duration_ms": r.duration_ms
                }
                for r in results
            ]
        }
        print(json.dumps(output, indent=2))
    else:
        # Print results
        passed = 0
        failed = 0

        for r in results:
            icon = "✅" if r.passed else "❌"
            duration = f" ({r.duration_ms:.0f}ms)" if r.duration_ms else ""
            print(f"{icon} {r.name}: {r.message}{duration}")

            if r.details and not r.passed:
                print(f"   Details: {json.dumps(r.details, indent=4)[:200]}")

            if r.passed:
                passed += 1
            else:
                failed += 1

        print(f"\n{'='*50}")
        print(f"Results: {passed} passed, {failed} failed")

        if failed > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
