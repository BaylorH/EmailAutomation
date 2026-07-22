import json
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path

from email_automation.claim_pipeline.legacy_shadow import (
    LegacyShadowIdentity,
    run_legacy_shadow,
)
from email_automation.claim_pipeline.legacy_shadow_fixtures import (
    LegacyShadowFixtureCatalog,
    LegacyShadowExpectation,
    load_legacy_shadow_fixture_catalog,
)
from email_automation.claim_pipeline.policy_fixtures import (
    load_policy_fixture_catalog,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SHADOW_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_legacy_shadow_cases.json"
POLICY_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_policy_cases.json"
SHADOW_SCRIPT = REPO_ROOT / "scripts" / "run_claim_pipeline_legacy_shadow.py"


class LegacyShadowReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy_catalog = load_policy_fixture_catalog(POLICY_FIXTURE_PATH)
        cls.shadow_catalog = load_legacy_shadow_fixture_catalog(
            SHADOW_FIXTURE_PATH,
            policy_catalog=cls.policy_catalog,
        )

    def _identity(self, **overrides):
        values = {
            "code_revision": "a" * 40,
            "source_tree_hash": "b" * 64,
            "source_tree_dirty": True,
            "python_version": "3.12.11",
            "dependency_lock_hash": "c" * 64,
            "policy_fixture_hash": self.policy_catalog.manifest_hash,
            "legacy_fixture_hash": self.shadow_catalog.manifest_hash,
            "repeats": 3,
            "case_count": len(self.shadow_catalog.cases),
        }
        values.update(overrides)
        return LegacyShadowIdentity.create(**values)

    def test_identity_is_stable_complete_and_tamper_evident(self):
        first = self._identity()
        second = self._identity()

        self.assertEqual(first, second)
        self.assertRegex(first.identity_id, r"^legacy_shadow_identity_[0-9a-f]{24}$")
        self.assertEqual(45, first.planned_comparisons)
        self.assertEqual(
            {
                "identityId",
                "codeRevision",
                "sourceTreeHash",
                "sourceTreeDirty",
                "pythonVersion",
                "dependencyLockHash",
                "policyFixtureHash",
                "legacyFixtureHash",
                "repeats",
                "caseCount",
                "plannedComparisons",
            },
            set(first.to_dict()),
        )
        with self.assertRaises(ValueError):
            replace(first, planned_comparisons=1)

    def test_identity_rejects_bad_hashes_and_repeat_bounds(self):
        for overrides in (
            {"code_revision": "not-a-revision"},
            {"source_tree_hash": "x" * 64},
            {"python_version": "private@example.test"},
            {"repeats": 0},
            {"repeats": 11},
            {"case_count": 0},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self._identity(**overrides)

    def test_three_repeat_report_is_stable_and_counts_findings(self):
        report = run_legacy_shadow(
            policy_catalog=self.policy_catalog,
            shadow_catalog=self.shadow_catalog,
            identity=self._identity(),
        )

        self.assertTrue(report.passed)
        self.assertEqual(1, len(set(report.repeat_digests)))
        self.assertEqual(15, len(report.case_results))
        self.assertEqual(9, report.release_blocker_case_count)
        self.assertEqual(25, report.discrepancy_count)
        self.assertEqual(
            {
                "equivalent": 1,
                "expected_improvement": 1,
                "deferred_surface": 4,
                "legacy_safety_risk": 7,
                "new_policy_gap": 2,
            },
            dict(report.disposition_counts),
        )
        self.assertEqual((), report.expectation_mismatch_case_ids)

    def test_reversed_fixture_order_produces_same_result_digest(self):
        forward = run_legacy_shadow(
            policy_catalog=self.policy_catalog,
            shadow_catalog=self.shadow_catalog,
            identity=self._identity(),
        )
        reversed_catalog = LegacyShadowFixtureCatalog(
            schema_version=self.shadow_catalog.schema_version,
            catalog_id=self.shadow_catalog.catalog_id,
            cases=tuple(reversed(self.shadow_catalog.cases)),
            manifest_hash=self.shadow_catalog.manifest_hash,
        )
        reverse = run_legacy_shadow(
            policy_catalog=self.policy_catalog,
            shadow_catalog=reversed_catalog,
            identity=self._identity(),
        )

        self.assertEqual(forward.result_digest, reverse.result_digest)
        self.assertEqual(forward.to_dict(), reverse.to_dict())

    def test_expected_result_drift_fails_the_report(self):
        first = self.shadow_catalog.cases[0]
        changed = replace(
            first,
            expected=LegacyShadowExpectation(
                disposition="equivalent",
                severity="none",
                discrepancy_codes=(),
            ),
        )
        drifted_catalog = LegacyShadowFixtureCatalog(
            schema_version=self.shadow_catalog.schema_version,
            catalog_id=self.shadow_catalog.catalog_id,
            cases=(changed, *self.shadow_catalog.cases[1:]),
            manifest_hash=self.shadow_catalog.manifest_hash,
        )

        report = run_legacy_shadow(
            policy_catalog=self.policy_catalog,
            shadow_catalog=drifted_catalog,
            identity=self._identity(),
        )

        self.assertFalse(report.passed)
        self.assertEqual((first.case_id,), report.expectation_mismatch_case_ids)

    def test_serialized_report_contains_no_proposal_or_customer_values(self):
        report = run_legacy_shadow(
            policy_catalog=self.policy_catalog,
            shadow_catalog=self.shadow_catalog,
            identity=self._identity(),
        )
        serialized = json.dumps(report.to_dict(), sort_keys=True)

        for forbidden in (
            "legacyProposal",
            "responseDraft",
            "example.test",
            "100 Target Rd",
            "900 Replacement Rd",
            "Hi broker",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_cli_emits_one_privacy_safe_json_report(self):
        completed = subprocess.run(
            [sys.executable, str(SHADOW_SCRIPT), "--repeats", "3"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["passed"])
        self.assertEqual(45, payload["identity"]["plannedComparisons"])
        self.assertEqual(9, payload["summary"]["releaseBlockerCaseCount"])
        self.assertNotIn("example.test", completed.stdout)


if __name__ == "__main__":
    unittest.main()
