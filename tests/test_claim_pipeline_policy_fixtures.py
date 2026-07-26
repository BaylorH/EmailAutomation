import json
import tempfile
import unittest
from pathlib import Path

from email_automation.claim_pipeline.policy_fixtures import (
    REQUIRED_POLICY_DIMENSIONS,
    PolicyFixtureValidationError,
    load_policy_fixture_catalog,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "claim_pipeline_policy_cases.json"
)


class PolicyFixtureTests(unittest.TestCase):
    def test_catalog_is_broad_strict_and_reproducible(self):
        first = load_policy_fixture_catalog(FIXTURE_PATH)
        second = load_policy_fixture_catalog(FIXTURE_PATH)

        self.assertGreaterEqual(len(first.cases), 19)
        self.assertTrue(REQUIRED_POLICY_DIMENSIONS <= first.covered_dimensions)
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.assertEqual(64, len(first.manifest_hash))
        for case in first.cases:
            self.assertEqual("no_side_effect", case.expected["effectPolicy"])
            self.assertTrue(case.expected["results"])

    def _mutated_payload(self):
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _assert_rejected(self, payload, pattern):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(PolicyFixtureValidationError, pattern):
                load_policy_fixture_catalog(path)

    def test_duplicate_case_ids_are_rejected(self):
        payload = self._mutated_payload()
        payload["cases"].append(payload["cases"][0])
        self._assert_rejected(payload, "duplicate caseId")

    def test_unknown_keys_are_rejected(self):
        payload = self._mutated_payload()
        payload["cases"][0]["allowSend"] = True
        self._assert_rejected(payload, "unknown keys")

    def test_missing_dimensions_are_rejected(self):
        payload = self._mutated_payload()
        for case in payload["cases"]:
            case["dimensions"] = [
                item for item in case["dimensions"] if item != "claim_conflict"
            ]
        self._assert_rejected(payload, "missing dimensions")

    def test_unknown_reason_code_is_rejected(self):
        payload = self._mutated_payload()
        payload["cases"][0]["expected"]["results"][0]["reasonCodes"] = ["surprise"]
        self._assert_rejected(payload, "unknown reason code")

    def test_malformed_current_state_is_rejected(self):
        payload = self._mutated_payload()
        payload["cases"][0]["currentState"] = []
        self._assert_rejected(payload, "currentState must be an object")

    def test_side_effect_policy_is_rejected(self):
        payload = self._mutated_payload()
        payload["cases"][0]["expected"]["effectPolicy"] = "send_email"
        self._assert_rejected(payload, "no_side_effect")


if __name__ == "__main__":
    unittest.main()

