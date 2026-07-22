import json
import tempfile
import unittest
from pathlib import Path

from email_automation.claim_pipeline.claim_fixtures import load_claim_fixture_catalog
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)
from email_automation.claim_pipeline.provider_quality_fixtures import (
    PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION,
    SUPPORTED_REVIEW_CATEGORIES,
    ProviderQualityFixtureValidationError,
    load_provider_quality_fixture_catalog,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
CLAIM_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_claim_cases.json"
INTERPRETATION_FIXTURE_PATH = (
    FIXTURE_ROOT / "claim_pipeline_interpretation_cases.json"
)
PROVIDER_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_provider_quality_cases.json"


class ProviderQualityFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.claims = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
        cls.interpretation = load_interpretation_fixture_catalog(
            INTERPRETATION_FIXTURE_PATH
        )

    def _load(self, path=PROVIDER_FIXTURE_PATH):
        return load_provider_quality_fixture_catalog(
            path,
            claim_catalog=self.claims,
            interpretation_catalog=self.interpretation,
        )

    def _mutated(self, mutate):
        raw = json.loads(PROVIDER_FIXTURE_PATH.read_text(encoding="utf-8"))
        mutate(raw)
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "provider-quality.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return directory, path

    def test_catalog_is_versioned_hashed_and_one_case_per_interpretation(self):
        first = self._load()
        second = self._load()

        self.assertEqual(1, PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION)
        self.assertEqual(PROVIDER_QUALITY_FIXTURE_SCHEMA_VERSION, first.schema_version)
        self.assertEqual(self.claims.manifest_hash, first.claim_fixture_hash)
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.assertEqual(19, len(first.cases))
        self.assertEqual(
            {case.case_id for case in self.interpretation.cases},
            {case.interpretation_case_id for case in first.cases},
        )
        self.assertEqual(
            {case.case_id for case in self.claims.cases},
            {
                source_case_id
                for case in first.cases
                for source_case_id in case.source_claim_case_ids
            },
        )

    def test_complete_claim_sets_are_the_union_for_each_unique_request(self):
        catalog = self._load()
        by_interpretation = {
            case.interpretation_case_id: case for case in catalog.cases
        }

        self.assertEqual(
            2,
            len(by_interpretation["split-suite-statuses"].expected_claim_digests),
        )
        self.assertEqual(
            13,
            len(by_interpretation["complete-property-facts"].expected_claim_digests),
        )
        self.assertEqual(
            ("043efbb5c2b3f68caa0ca2fb21b4f99935a08acf5a2da781ae87602574866747",),
            by_interpretation[
                "outlook-original-message-history"
            ].expected_claim_digests,
        )

    def test_catalog_rejects_claim_fixture_hash_drift(self):
        directory, path = self._mutated(
            lambda raw: raw.__setitem__("claimFixtureHash", "0" * 64)
        )
        with directory:
            with self.assertRaisesRegex(
                ProviderQualityFixtureValidationError,
                "claim fixture hash",
            ):
                self._load(path)

    def test_source_cases_must_form_an_exact_partition(self):
        mutations = (
            lambda raw: raw["cases"][0]["sourceClaimCaseIds"].pop(),
            lambda raw: raw["cases"][1]["sourceClaimCaseIds"].append(
                raw["cases"][0]["sourceClaimCaseIds"][0]
            ),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                directory, path = self._mutated(mutate)
                with directory:
                    with self.assertRaises(ProviderQualityFixtureValidationError):
                        self._load(path)

    def test_grouped_source_cases_must_have_identical_request_inputs(self):
        def mutate(raw):
            moved = raw["cases"][1]["sourceClaimCaseIds"].pop()
            raw["cases"][0]["sourceClaimCaseIds"].append(moved)
            raw["cases"][0]["sourceClaimCaseIds"].sort()

        directory, path = self._mutated(mutate)
        with directory:
            with self.assertRaisesRegex(
                ProviderQualityFixtureValidationError,
                "request-equivalent",
            ):
                self._load(path)

    def test_expected_claim_digests_cannot_be_weakened_or_invented(self):
        mutations = (
            lambda raw: raw["cases"][0].__setitem__("expectedClaimDigests", []),
            lambda raw: raw["cases"][0].__setitem__(
                "expectedClaimDigests",
                sorted(raw["cases"][0]["expectedClaimDigests"] + ["0" * 64]),
            ),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                directory, path = self._mutated(mutate)
                with directory:
                    with self.assertRaisesRegex(
                        ProviderQualityFixtureValidationError,
                        "complete accepted claim union",
                    ):
                        self._load(path)

    def test_review_expectations_are_bounded_and_evidence_bound(self):
        self.assertEqual(
            {"entity_ambiguity", "insufficient_evidence"},
            set(SUPPORTED_REVIEW_CATEGORIES),
        )
        mutations = (
            lambda raw: raw["cases"][7]["expectedReviews"][0].__setitem__(
                "category", "private free-form reason"
            ),
            lambda raw: raw["cases"][7]["expectedReviews"][0].__setitem__(
                "evidenceIndex", 999
            ),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                directory, path = self._mutated(mutate)
                with directory:
                    with self.assertRaises(ProviderQualityFixtureValidationError):
                        self._load(path)

    def test_unknown_keys_and_duplicate_provider_cases_fail_closed(self):
        mutations = (
            lambda raw: raw.__setitem__("unknown", True),
            lambda raw: raw["cases"][0].__setitem__("unknown", True),
            lambda raw: raw["cases"].append(dict(raw["cases"][0])),
        )

        for mutate in mutations:
            with self.subTest(mutation=mutate):
                directory, path = self._mutated(mutate)
                with directory:
                    with self.assertRaises(ProviderQualityFixtureValidationError):
                        self._load(path)


if __name__ == "__main__":
    unittest.main()
