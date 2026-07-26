import json
import tempfile
import unittest
from pathlib import Path

from email_automation.claim_pipeline.legacy_shadow_fixtures import (
    LegacyShadowFixtureValidationError,
    load_legacy_shadow_fixture_catalog,
)
from email_automation.claim_pipeline.policy_fixtures import (
    load_policy_fixture_catalog,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SHADOW_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_legacy_shadow_cases.json"
POLICY_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_policy_cases.json"


class LegacyShadowFixtureTests(unittest.TestCase):
    def setUp(self):
        self.policy_catalog = load_policy_fixture_catalog(POLICY_FIXTURE_PATH)

    def _load_payload(self):
        return json.loads(SHADOW_FIXTURE_PATH.read_text(encoding="utf-8"))

    def _write_and_load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_legacy_shadow_fixture_catalog(
                path,
                policy_catalog=self.policy_catalog,
            )

    def test_catalog_is_strict_cross_referenced_and_hashed(self):
        catalog = load_legacy_shadow_fixture_catalog(
            SHADOW_FIXTURE_PATH,
            policy_catalog=self.policy_catalog,
        )

        self.assertEqual(1, catalog.schema_version)
        self.assertEqual(15, len(catalog.cases))
        self.assertEqual(64, len(catalog.manifest_hash))
        self.assertEqual(
            {case.policy_case_id for case in catalog.cases},
            {
                "available-missing-facts",
                "explicit-unavailable",
                "tour-only-unavailable-near-miss",
                "hard-occupancy-miss",
                "accepting-backups",
                "split-suite-mixed",
                "alternate-property-isolation",
                "complete-required-facts",
                "out-of-office-return",
                "redirect-requires-approval",
                "opt-out-plus-call",
                "conflicting-availability",
                "unsupported-hard-requirement",
            },
        )

    def test_unknown_keys_fail_closed(self):
        payload = self._load_payload()
        payload["cases"][0]["rawMessage"] = "customer content"

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "unknown keys"
        ):
            self._write_and_load(payload)

    def test_raw_update_values_are_not_allowed(self):
        payload = self._load_payload()
        payload["cases"][0]["legacyProposal"]["updates"][0]["value"] = "available"

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "unknown keys"
        ):
            self._write_and_load(payload)

    def test_response_bodies_and_recipient_values_are_not_part_of_schema(self):
        payload = self._load_payload()
        proposal = payload["cases"][0]["legacyProposal"]
        proposal["responseEmail"] = "Hi broker"
        proposal["recipient"] = "person@example.test"

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "unknown keys"
        ):
            self._write_and_load(payload)

    def test_every_event_requires_an_explicit_entity_binding(self):
        payload = self._load_payload()
        payload["cases"][1]["bindings"]["eventEntities"] = []

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "eventEntities"
        ):
            self._write_and_load(payload)

    def test_entity_bindings_must_reference_the_policy_case(self):
        payload = self._load_payload()
        payload["cases"][0]["bindings"]["currentEntity"] = "other"

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "unknown policy entity"
        ):
            self._write_and_load(payload)

    def test_unknown_policy_case_is_rejected(self):
        payload = self._load_payload()
        payload["cases"][0]["policyCaseId"] = "not-a-policy-case"

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "unknown policy case"
        ):
            self._write_and_load(payload)

    def test_provenance_reference_is_report_safe(self):
        payload = self._load_payload()
        payload["cases"][0]["provenance"]["sourceRef"] = "tests/a.py:1 customer@example.test"

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "report-safe"
        ):
            self._write_and_load(payload)

    def test_synthetic_boundary_cannot_claim_a_live_source(self):
        payload = self._load_payload()
        case = payload["cases"][2]
        case["provenance"] = {
            "kind": "synthetic_boundary",
            "sourceRef": "live-probe-17",
        }

        with self.assertRaisesRegex(
            LegacyShadowFixtureValidationError, "synthetic sourceRef"
        ):
            self._write_and_load(payload)

    def test_manifest_hash_changes_with_expected_classification(self):
        first = load_legacy_shadow_fixture_catalog(
            SHADOW_FIXTURE_PATH,
            policy_catalog=self.policy_catalog,
        )
        payload = self._load_payload()
        payload["cases"][0]["expected"]["severity"] = "info"
        second = self._write_and_load(payload)

        self.assertNotEqual(first.manifest_hash, second.manifest_hash)


if __name__ == "__main__":
    unittest.main()
