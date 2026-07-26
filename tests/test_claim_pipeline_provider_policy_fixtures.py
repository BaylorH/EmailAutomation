import copy
import json
import tempfile
import unittest
from pathlib import Path

from email_automation.claim_pipeline.claim_fixtures import (
    load_claim_fixture_catalog,
)
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)
from email_automation.claim_pipeline.provider_policy_fixtures import (
    ProviderPolicyFixtureValidationError,
    load_provider_policy_fixture_catalog,
)
from email_automation.claim_pipeline.provider_quality_fixtures import (
    load_provider_quality_fixture_catalog,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
INTERPRETATION_PATH = FIXTURE_ROOT / "claim_pipeline_interpretation_cases.json"
CLAIM_PATH = FIXTURE_ROOT / "claim_pipeline_claim_cases.json"
PROVIDER_QUALITY_PATH = FIXTURE_ROOT / "claim_pipeline_provider_quality_cases.json"
PROVIDER_POLICY_PATH = FIXTURE_ROOT / "claim_pipeline_provider_policy_cases.json"


class ProviderPolicyFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interpretation_catalog = load_interpretation_fixture_catalog(
            INTERPRETATION_PATH
        )
        cls.claim_catalog = load_claim_fixture_catalog(CLAIM_PATH)
        cls.provider_quality_catalog = load_provider_quality_fixture_catalog(
            PROVIDER_QUALITY_PATH,
            claim_catalog=cls.claim_catalog,
            interpretation_catalog=cls.interpretation_catalog,
        )
        cls.raw = json.loads(PROVIDER_POLICY_PATH.read_text(encoding="utf-8"))

    def _load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_provider_policy_fixture_catalog(
                path,
                provider_quality_catalog=self.provider_quality_catalog,
            )

    def test_catalog_is_stable_complete_and_cross_catalog_bound(self):
        first = load_provider_policy_fixture_catalog(
            PROVIDER_POLICY_PATH,
            provider_quality_catalog=self.provider_quality_catalog,
        )
        second = load_provider_policy_fixture_catalog(
            PROVIDER_POLICY_PATH,
            provider_quality_catalog=self.provider_quality_catalog,
        )

        self.assertEqual(8, len(first.cases))
        self.assertEqual(first, second)
        self.assertEqual(
            self.provider_quality_catalog.manifest_hash,
            first.provider_quality_fixture_hash,
        )
        self.assertEqual(64, len(first.manifest_hash))
        self.assertEqual(
            {
                "attachment_isolation",
                "complete_facts",
                "correction",
                "freshness",
                "repeated_request",
                "split_suite",
                "terminal_suppression",
                "workflow_intents",
            },
            first.covered_dimensions,
        )

    def test_unknown_keys_and_provider_references_fail_closed(self):
        payload = copy.deepcopy(self.raw)
        payload["cases"][0]["message"] = "raw text"
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "unknown"):
            self._load(payload)

        payload = copy.deepcopy(self.raw)
        payload["cases"][0]["providerCaseId"] = "unknown-provider-case"
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "unknown"):
            self._load(payload)

        payload = copy.deepcopy(self.raw)
        payload["providerQualityFixtureHash"] = "0" * 64
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "hash"):
            self._load(payload)

    def test_duplicate_case_provider_and_subject_selectors_fail_closed(self):
        payload = copy.deepcopy(self.raw)
        payload["cases"][1]["caseId"] = payload["cases"][0]["caseId"]
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "duplicate"):
            self._load(payload)

        payload = copy.deepcopy(self.raw)
        payload["cases"][1]["providerCaseId"] = payload["cases"][0][
            "providerCaseId"
        ]
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "duplicate"):
            self._load(payload)

        payload = copy.deepcopy(self.raw)
        payload["cases"][1]["subjects"].append(
            copy.deepcopy(payload["cases"][1]["subjects"][0])
        )
        payload["cases"][1]["subjects"][-1]["key"] = "duplicate-selector"
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "selector"):
            self._load(payload)

    def test_raw_contact_address_and_evidence_fields_are_rejected(self):
        unsafe_values = (
            ("evidenceText", "The broker wrote private words"),
            ("recipient", "broker@example.test"),
            ("canonicalAddress", "123 Industrial Avenue"),
            ("rawOutput", {"claims": []}),
        )
        for key, value in unsafe_values:
            with self.subTest(key=key):
                payload = copy.deepcopy(self.raw)
                payload["cases"][0][key] = value
                with self.assertRaisesRegex(
                    ProviderPolicyFixtureValidationError,
                    "unknown|unsafe",
                ):
                    self._load(payload)

    def test_expected_results_and_gap_codes_are_strict(self):
        payload = copy.deepcopy(self.raw)
        payload["cases"][0]["expected"]["results"][0]["requiredActions"] = [
            "not_an_action:automatic"
        ]
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "invalid"):
            self._load(payload)

        payload = copy.deepcopy(self.raw)
        payload["cases"][0]["expected"]["gapCodes"] = ["ignore-the-difference"]
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "gap"):
            self._load(payload)

        payload = copy.deepcopy(self.raw)
        payload["cases"][0]["expected"]["disposition"] = "expected_gap"
        payload["cases"][0]["expected"]["gapCodes"] = []
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "gap"):
            self._load(payload)

    def test_current_state_references_declared_subjects_only(self):
        payload = copy.deepcopy(self.raw)
        payload["cases"][0]["currentState"]["facts"]["missing-subject"] = {}
        with self.assertRaisesRegex(ProviderPolicyFixtureValidationError, "subject"):
            self._load(payload)


if __name__ == "__main__":
    unittest.main()
