import json
import tempfile
import unittest
from pathlib import Path

from email_automation.claim_pipeline.fixtures import (
    REQUIRED_DIMENSIONS,
    FixtureValidationError,
    load_fixture_catalog,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "claim_pipeline_boundary_cases.json"
)


class ClaimPipelineFixtureTests(unittest.TestCase):
    def test_boundary_catalog_covers_each_governing_dimension(self):
        catalog = load_fixture_catalog(FIXTURE_PATH)

        self.assertGreaterEqual(len(catalog.cases), 12)
        self.assertTrue(REQUIRED_DIMENSIONS.issubset(catalog.covered_dimensions))

    def test_every_case_has_stage_level_oracles(self):
        catalog = load_fixture_catalog(FIXTURE_PATH)
        required_outcomes = {
            "evidenceCount",
            "entities",
            "claims",
            "decisions",
            "actions",
            "effectPolicy",
        }

        for case in catalog.cases:
            with self.subTest(case_id=case.case_id):
                self.assertTrue(required_outcomes.issubset(case.expected))

    def test_split_suite_case_preserves_separate_entities_claims_and_decisions(self):
        catalog = load_fixture_catalog(FIXTURE_PATH)
        case = next(
            item for item in catalog.cases if item.case_id == "split-suite-availability"
        )

        self.assertEqual(
            {"suite_a", "suite_b"},
            {item["entityKey"] for item in case.expected["entities"]},
        )
        self.assertEqual(
            {"suite_a", "suite_b"},
            {item["subject"] for item in case.expected["decisions"]},
        )
        self.assertEqual(
            [("suite_a", "unavailable"), ("suite_b", "available")],
            [
                (item["subject"], item["value"])
                for item in case.expected["claims"]
                if item["predicate"] == "availability"
            ],
        )

    def test_manifest_hash_is_reproducible(self):
        first = load_fixture_catalog(FIXTURE_PATH)
        second = load_fixture_catalog(FIXTURE_PATH)

        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.assertEqual(64, len(first.manifest_hash))

    def test_duplicate_case_ids_are_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"].append(payload["cases"][0])

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "duplicate.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "duplicate caseId"):
                load_fixture_catalog(path)

    def test_unknown_root_keys_are_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["allowSideEffects"] = True

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unknown-key.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "unknown root keys"):
                load_fixture_catalog(path)

    def test_missing_governing_dimension_is_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for case in payload["cases"]:
            case["dimensions"] = [
                value
                for value in case["dimensions"]
                if value != "commit_state"
            ]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing-dimension.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "missing dimensions"):
                load_fixture_catalog(path)

    def test_unknown_expected_keys_are_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"][0]["expected"]["finalLabel"] = "pass"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unknown-expected-key.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "unknown keys"):
                load_fixture_catalog(path)

    def test_claim_oracle_cannot_reference_an_unknown_entity(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"][0]["expected"]["claims"][0]["subject"] = "wrong_property"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unknown-entity.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "unknown subject"):
                load_fixture_catalog(path)

    def test_invalid_stage_enum_is_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"][0]["expected"]["actions"][0]["approvalClass"] = "probably"

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid-enum.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "invalid value"):
                load_fixture_catalog(path)

    def test_unknown_contract_input_is_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"][0]["contract"]["notAContractField"] = True

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid-contract.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "unknown keys"):
                load_fixture_catalog(path)

    def test_malformed_evidence_input_is_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"][0]["evidence"][0] = {"garbage": True}

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "invalid-evidence.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "missing keys"):
                load_fixture_catalog(path)

    def test_non_string_evidence_text_is_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"][0]["evidence"][0]["text"] = 123

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "numeric-evidence-text.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "must be a string"):
                load_fixture_catalog(path)

    def test_non_string_policy_value_is_rejected(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        payload["cases"][6]["contract"]["outOfOfficePolicy"] = True

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "boolean-policy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(FixtureValidationError, "must be a string"):
                load_fixture_catalog(path)


if __name__ == "__main__":
    unittest.main()
