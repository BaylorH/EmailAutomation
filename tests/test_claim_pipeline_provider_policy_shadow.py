import json
import unittest
from dataclasses import replace
from pathlib import Path

from email_automation.claim_pipeline.claim_fixtures import (
    load_claim_fixture_catalog,
)
from email_automation.claim_pipeline.extraction import CLAIM_EXTRACTION_SCHEMA_VERSION
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)
from email_automation.claim_pipeline.provider_policy_fixtures import (
    load_provider_policy_fixture_catalog,
)
from email_automation.claim_pipeline.provider_policy_shadow import (
    ProviderPolicyShadowIdentity,
    RecordedProviderQualityProposalAdapter,
    run_provider_policy_shadow,
)
from email_automation.claim_pipeline.provider_quality_fixtures import (
    load_provider_quality_fixture_catalog,
)
from email_automation.claim_pipeline.replay import ProposalResponse


FIXTURE_ROOT = Path(__file__).parent / "fixtures"


class _TamperedAdapter:
    def __init__(self, delegate, case_id):
        self.provider_id = delegate.provider_id
        self.model_id = delegate.model_id
        self.prompt_id = delegate.prompt_id
        self.prompt_hash = delegate.prompt_hash
        self._delegate = delegate
        self._case_id = case_id
        self.calls = 0

    def propose(self, **kwargs):
        self.calls += 1
        response = self._delegate.propose(**kwargs)
        if kwargs["case_id"] != self._case_id:
            return response
        output = dict(response.model_output)
        output["claims"] = list(output["claims"][:-1])
        return ProposalResponse(model_output=output, usage=response.usage)


class ProviderPolicyShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.interpretation_catalog = load_interpretation_fixture_catalog(
            FIXTURE_ROOT / "claim_pipeline_interpretation_cases.json"
        )
        cls.claim_catalog = load_claim_fixture_catalog(
            FIXTURE_ROOT / "claim_pipeline_claim_cases.json"
        )
        cls.provider_quality_catalog = load_provider_quality_fixture_catalog(
            FIXTURE_ROOT / "claim_pipeline_provider_quality_cases.json",
            claim_catalog=cls.claim_catalog,
            interpretation_catalog=cls.interpretation_catalog,
        )
        cls.provider_policy_catalog = load_provider_policy_fixture_catalog(
            FIXTURE_ROOT / "claim_pipeline_provider_policy_cases.json",
            provider_quality_catalog=cls.provider_quality_catalog,
        )

    def _identity(self, adapter, *, repeats=1, dirty=False):
        return ProviderPolicyShadowIdentity.create(
            code_revision="a" * 40,
            source_tree_hash="b" * 64,
            source_tree_dirty=dirty,
            python_version="3.12.11",
            dependency_lock_hash="c" * 64,
            interpretation_fixture_hash=self.interpretation_catalog.manifest_hash,
            claim_fixture_hash=self.claim_catalog.manifest_hash,
            provider_quality_fixture_hash=self.provider_quality_catalog.manifest_hash,
            provider_policy_fixture_hash=self.provider_policy_catalog.manifest_hash,
            extraction_schema_version=CLAIM_EXTRACTION_SCHEMA_VERSION,
            provider_id=adapter.provider_id,
            model_id=adapter.model_id,
            prompt_id=adapter.prompt_id,
            prompt_hash=adapter.prompt_hash,
            repeats=repeats,
            case_count=len(self.provider_policy_catalog.cases),
        )

    def _run(self, *, repeats=1, adapter=None, catalog=None, dirty=False):
        adapter = adapter or RecordedProviderQualityProposalAdapter(
            self.provider_quality_catalog,
            self.claim_catalog,
        )
        catalog = catalog or self.provider_policy_catalog
        return run_provider_policy_shadow(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            provider_quality_catalog=self.provider_quality_catalog,
            provider_policy_catalog=catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=repeats, dirty=dirty),
        )

    def test_recorded_composition_matches_exact_policy_oracles_without_usage(self):
        report = self._run(repeats=3)

        self.assertTrue(report.evaluation_passed)
        self.assertTrue(report.passed)
        self.assertEqual(24, len(report.results))
        self.assertEqual(0, report.provider_calls)
        self.assertEqual(0, report.total_tokens)
        self.assertEqual(0, report.cost_microusd)
        self.assertEqual((), report.policy_outcome_variance_case_ids)
        self.assertFalse(
            any(item.gap_codes for item in report.results),
            [
                (item.case_id, item.gap_codes)
                for item in report.results
                if item.gap_codes
            ],
        )

    def test_policy_results_preserve_entity_and_terminal_boundaries(self):
        report = self._run()
        by_id = {item.case_id: item for item in report.results}

        split = by_id["split-suite-isolation"]
        self.assertEqual(("suite-a", "suite-b"), split.subject_keys)
        self.assertEqual("pass", split.disposition)
        self.assertEqual((), split.mismatch_codes)

        alternate = by_id["attachment-alternate-isolation"]
        self.assertEqual(("alternate",), alternate.subject_keys)
        self.assertEqual("pass", alternate.disposition)

        unavailable = by_id["unavailable-optout-suppression"]
        self.assertEqual("pass", unavailable.disposition)
        self.assertEqual((), unavailable.gap_codes)

        correction = by_id["rent-correction-closeout"]
        self.assertEqual("pass", correction.disposition)

    def test_provider_quality_failure_prevents_policy_pass(self):
        delegate = RecordedProviderQualityProposalAdapter(
            self.provider_quality_catalog,
            self.claim_catalog,
        )
        adapter = _TamperedAdapter(delegate, "target-unavailable-stop-followups")
        report = self._run(adapter=adapter)

        self.assertFalse(report.evaluation_passed)
        self.assertFalse(report.passed)
        self.assertEqual(6, adapter.calls)
        failed = next(
            item
            for item in report.results
            if item.case_id == "unavailable-optout-suppression"
        )
        self.assertIn("provider_quality_failed", failed.mismatch_codes)
        self.assertIn(
            "missing_expected_claims",
            failed.provider_quality_mismatch_codes,
        )
        stopped = next(
            item
            for item in report.results
            if item.case_id == "workflow-intents-visible"
        )
        self.assertEqual(("not_run_after_failure",), stopped.mismatch_codes)
        self.assertEqual((), report.policy_outcome_variance_case_ids)

    def test_fixture_order_does_not_change_semantic_result_digest(self):
        baseline = self._run()
        reversed_catalog = replace(
            self.provider_policy_catalog,
            cases=tuple(reversed(self.provider_policy_catalog.cases)),
        )
        reversed_report = self._run(catalog=reversed_catalog)

        self.assertEqual(baseline.result_digest, reversed_report.result_digest)
        self.assertEqual(
            [item.to_dict() for item in baseline.results],
            [item.to_dict() for item in reversed_report.results],
        )

    def test_dirty_tree_keeps_evidence_visible_but_fails_final_gate(self):
        report = self._run(dirty=True)

        self.assertTrue(report.evaluation_passed)
        self.assertFalse(report.passed)

    def test_report_is_value_free(self):
        serialized = json.dumps(self._run().to_dict(), sort_keys=True)

        for forbidden in (
            "123 Industrial",
            "999 Other",
            "alex@example",
            "jordan@example",
            "$15.50",
            "roof leak",
            "evidenceText",
            "rawOutput",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
