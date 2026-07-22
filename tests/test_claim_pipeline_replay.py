import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from email_automation.claim_pipeline.claim_fixtures import load_claim_fixture_catalog
from email_automation.claim_pipeline.interpretation_fixtures import (
    load_interpretation_fixture_catalog,
)
from email_automation.claim_pipeline.provider_quality_fixtures import (
    load_provider_quality_fixture_catalog,
)
from email_automation.claim_pipeline.extraction import CLAIM_EXTRACTION_SCHEMA_VERSION
from email_automation.claim_pipeline.replay import (
    MAX_REPLAY_CALLS,
    ProviderTelemetrySnapshot,
    ProposalResponse,
    ProposalUsage,
    RecordedProposalAdapter,
    ReplayIdentity,
    run_claim_replay,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
CLAIM_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_claim_cases.json"
INTERPRETATION_FIXTURE_PATH = (
    FIXTURE_ROOT / "claim_pipeline_interpretation_cases.json"
)
PROVIDER_QUALITY_FIXTURE_PATH = (
    FIXTURE_ROOT / "claim_pipeline_provider_quality_cases.json"
)
REPO_ROOT = Path(__file__).resolve().parents[1]
REPLAY_SCRIPT = REPO_ROOT / "scripts" / "run_claim_pipeline_replay.py"


class ReplayContractTests(unittest.TestCase):
    def _identity_values(self, **overrides):
        values = {
            "code_revision": "a" * 40,
            "source_tree_hash": "f" * 64,
            "source_tree_dirty": True,
            "python_version": "3.12.11",
            "dependency_lock_hash": "b" * 64,
            "interpretation_fixture_hash": "c" * 64,
            "claim_fixture_hash": "d" * 64,
            "evaluation_fixture_hash": "d" * 64,
            "extraction_schema_version": 1,
            "provider_id": "recorded",
            "model_id": "fixture-output-v1",
            "prompt_id": "recorded-claim-proposal-v1",
            "prompt_hash": "e" * 64,
            "evaluation_profile": "candidate_validation",
            "repeats": 3,
            "case_count": 20,
            "interpretation_case_count": 14,
        }
        values.update(overrides)
        return values

    def test_identity_is_stable_complete_and_json_safe(self):
        first = ReplayIdentity.create(**self._identity_values())
        second = ReplayIdentity.create(**self._identity_values())

        self.assertEqual(first, second)
        self.assertRegex(first.identity_id, r"^replay_identity_[0-9a-f]{24}$")
        self.assertEqual(60, first.planned_calls)
        self.assertEqual(42, first.planned_interpretations)
        self.assertEqual(
            {
                "identityId",
                "codeRevision",
                "sourceTreeHash",
                "sourceTreeDirty",
                "pythonVersion",
                "dependencyLockHash",
                "interpretationFixtureHash",
                "claimFixtureHash",
                "evaluationFixtureHash",
                "extractionSchemaVersion",
                "providerId",
                "modelId",
                "promptId",
                "promptHash",
                "evaluationProfile",
                "repeats",
                "caseCount",
                "interpretationCaseCount",
                "plannedCalls",
                "plannedInterpretations",
            },
            set(first.to_dict()),
        )

    def test_identity_rejects_invalid_hashes_repeats_and_unbounded_calls(self):
        invalid_values = (
            {"code_revision": "private-broker@example.com"},
            {"dependency_lock_hash": "not-a-hash"},
            {"provider_id": "private-broker@example.com"},
            {"repeats": 0},
            {"repeats": 11},
            {"case_count": 0},
            {"case_count": MAX_REPLAY_CALLS + 1, "repeats": 1},
        )

        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    ReplayIdentity.create(**self._identity_values(**overrides))

    def test_identity_rejects_direct_tampering_after_construction(self):
        identity = ReplayIdentity.create(**self._identity_values())

        mutations = (
            {"identity_id": "replay_identity_" + "0" * 24},
            {"planned_calls": 1},
            {"planned_interpretations": 1},
            {"repeats": 11, "planned_calls": 220, "planned_interpretations": 154},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    replace(identity, **mutation)

    def test_usage_and_proposal_response_are_nonnegative_and_immutable(self):
        usage = ProposalUsage(
            input_tokens=11,
            output_tokens=7,
            latency_ms=25,
            cost_microusd=19,
            provider_calls=1,
            provider_billed=True,
            usage_complete=True,
        )
        response = ProposalResponse(
            model_output={"claims": [], "review": []},
            usage=usage,
        )

        self.assertEqual(18, usage.total_tokens)
        self.assertTrue(usage.provider_billed)
        self.assertTrue(usage.usage_complete)
        self.assertEqual({"claims": [], "review": []}, response.model_output)
        for field in ("input_tokens", "output_tokens", "latency_ms", "cost_microusd"):
            with self.subTest(field=field):
                values = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "latency_ms": 0,
                    "cost_microusd": 0,
                    "provider_calls": 0,
                    "provider_billed": False,
                    "usage_complete": True,
                }
                values[field] = -1
                with self.assertRaises(ValueError):
                    ProposalUsage(**values)
        with self.assertRaises(ValueError):
            ProposalUsage(cost_microusd=1, provider_billed=False)
        with self.assertRaises(ValueError):
            ProposalUsage(provider_calls=0, provider_billed=True)

    def test_provider_telemetry_delta_is_exact_and_rejects_regression(self):
        before = ProviderTelemetrySnapshot()
        after = ProviderTelemetrySnapshot(
            attempts=1,
            billed_calls=1,
            input_tokens=11,
            output_tokens=7,
            latency_ms=25,
            cost_microusd=19,
            incomplete_attempts=0,
        )

        self.assertEqual(
            ProposalUsage(
                input_tokens=11,
                output_tokens=7,
                latency_ms=25,
                cost_microusd=19,
                provider_calls=1,
                provider_billed=True,
                usage_complete=True,
            ),
            after.delta_usage(before),
        )
        with self.assertRaises(ValueError):
            before.delta_usage(after)


class _MutableTelemetry:
    def __init__(self):
        self._snapshot = ProviderTelemetrySnapshot()

    def snapshot(self):
        return self._snapshot

    def record(self, usage, *, attempts=None):
        observed_attempts = usage.provider_calls if attempts is None else attempts
        self._snapshot = ProviderTelemetrySnapshot(
            attempts=self._snapshot.attempts + observed_attempts,
            billed_calls=self._snapshot.billed_calls
            + (observed_attempts if usage.provider_billed else 0),
            input_tokens=self._snapshot.input_tokens + usage.input_tokens,
            output_tokens=self._snapshot.output_tokens + usage.output_tokens,
            latency_ms=self._snapshot.latency_ms + usage.latency_ms,
            cost_microusd=self._snapshot.cost_microusd + usage.cost_microusd,
            incomplete_attempts=self._snapshot.incomplete_attempts
            + (0 if usage.usage_complete else observed_attempts),
        )


class _AdapterWrapper:
    def __init__(
        self,
        recorded,
        *,
        usage=None,
        fail_case="",
        vary_case="",
        invalid_response_case="",
        wrong_case="",
        confidence_case="",
        wrong_review_evidence_case="",
        empty_case="",
        provider_id="",
        telemetry=None,
        observed_usage=None,
        observed_attempts=None,
    ):
        self._recorded = recorded
        self._usage = usage
        self._fail_case = fail_case
        self._vary_case = vary_case
        self._invalid_response_case = invalid_response_case
        self._wrong_case = wrong_case
        self._confidence_case = confidence_case
        self._wrong_review_evidence_case = wrong_review_evidence_case
        self._empty_case = empty_case
        self._telemetry = telemetry
        self._observed_usage = observed_usage
        self._observed_attempts = observed_attempts
        self._case_calls = {}
        self.requests_by_case = {}
        self.responses_by_case = {}
        self.calls = 0
        self.provider_id = recorded.provider_id
        if provider_id:
            self.provider_id = provider_id
        self.model_id = recorded.model_id
        self.prompt_id = recorded.prompt_id
        self.prompt_hash = recorded.prompt_hash

    def propose(self, *, case_id, request, evidence, entities):
        self.calls += 1
        self._case_calls[case_id] = self._case_calls.get(case_id, 0) + 1
        self.requests_by_case[case_id] = request
        if case_id == self._fail_case:
            if self._telemetry is not None:
                self._telemetry.record(
                    self._observed_usage
                    or ProposalUsage(
                        provider_calls=1,
                        provider_billed=False,
                        usage_complete=False,
                    ),
                    attempts=self._observed_attempts,
                )
            raise RuntimeError("private-broker@example.com must not reach the report")
        if case_id == self._invalid_response_case:
            return {"private": "broker@example.com"}
        response = self._recorded.propose(
            case_id=case_id,
            request=request,
            evidence=evidence,
            entities=entities,
        )
        output = json.loads(json.dumps(response.model_output))
        if case_id == self._empty_case:
            output = {"claims": [], "review": []}
        if (
            case_id == self._vary_case
            and self._case_calls[case_id] == 2
            and output["claims"]
        ):
            output["claims"][0]["confidence"] = max(
                0.01, output["claims"][0]["confidence"] - 0.01
            )
        if case_id == self._wrong_case and output["claims"]:
            output["claims"][0]["value"] = "definitely-wrong"
        if case_id == self._confidence_case and output["claims"]:
            output["claims"][0]["confidence"] = 0.81
        if case_id == self._wrong_review_evidence_case and output["review"]:
            current = output["review"][0]["evidenceId"]
            replacement = next(
                item.evidence_id for item in evidence if item.evidence_id != current
            )
            output["review"][0]["evidenceId"] = replacement
        result = ProposalResponse(
            model_output=output,
            usage=self._usage or response.usage,
        )
        if self._telemetry is not None:
            self._telemetry.record(
                self._observed_usage or result.usage,
                attempts=self._observed_attempts,
            )
        self.responses_by_case[case_id] = result
        return result


class _ProviderQualityRecordedAdapter:
    provider_id = "test-provider"
    model_id = "fixture-provider-v1"
    prompt_id = "fixture-provider-prompt-v1"
    prompt_hash = "9" * 64

    def __init__(
        self,
        provider_catalog,
        claim_catalog,
        telemetry,
        *,
        missing_claim_case="",
        unexpected_review_case="",
        invalid_review_category_case="",
        wrong_review_evidence_case="",
        rejected_candidate_case="",
        detail_mismatch_case="",
        nonsemantic_variation_case="",
        additional_accepted_claim_case="",
    ):
        self._provider_cases = {
            case.case_id: case for case in provider_catalog.cases
        }
        self._claim_cases = {case.case_id: case for case in claim_catalog.cases}
        self._telemetry = telemetry
        self._missing_claim_case = missing_claim_case
        self._unexpected_review_case = unexpected_review_case
        self._invalid_review_category_case = invalid_review_category_case
        self._wrong_review_evidence_case = wrong_review_evidence_case
        self._rejected_candidate_case = rejected_candidate_case
        self._detail_mismatch_case = detail_mismatch_case
        self._nonsemantic_variation_case = nonsemantic_variation_case
        self._additional_accepted_claim_case = additional_accepted_claim_case
        self.calls = 0

    def propose(self, *, case_id, request, evidence, entities):
        def plain(value):
            if hasattr(value, "items"):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [plain(item) for item in value]
            return value

        self.calls += 1
        case = self._provider_cases[case_id]
        entity_by_key = {
            (item.relationship, item.suite, item.canonical_address): item
            for item in entities
        }

        def materialize(raw):
            subject = raw["subject"]
            entity = entity_by_key[
                (
                    subject["relationship"],
                    subject["suite"],
                    subject["canonicalAddress"],
                )
            ]
            claim = {
                key: plain(value)
                for key, value in raw.items()
                if key not in {"evidenceIndex", "subject"}
            }
            supersedes = claim["supersedesClaimId"]
            if isinstance(supersedes, str) and supersedes.startswith("prior:"):
                claim["supersedesClaimId"] = request.prior_claims[
                    int(supersedes.removeprefix("prior:"))
                ].claim_id
            claim.update(
                {
                    "evidenceId": evidence[raw["evidenceIndex"]].evidence_id,
                    "subjectEntityId": entity.entity_id,
                }
            )
            return claim

        claims = {}
        for source_case_id in case.source_claim_case_ids:
            source = self._claim_cases[source_case_id]
            for index in source.expected["acceptedClaimIndexes"]:
                raw = source.claims[index]
                claim = materialize(raw)
                claims[json.dumps(claim, sort_keys=True)] = claim
        model_claims = list(claims.values())
        if case_id == self._missing_claim_case and model_claims:
            model_claims.pop()
        if case_id == self._detail_mismatch_case and model_claims:
            target = model_claims[0]
            target["effectiveAt"] = "2026-07-22"
        if case_id == self._nonsemantic_variation_case and model_claims:
            target = model_claims[0]
            target["evidenceText"] = next(
                item.content
                for item in evidence
                if item.evidence_id == target["evidenceId"]
            )
            target["confidence"] = 0.91
            numeric = next(
                (
                    claim
                    for claim in model_claims
                    if isinstance(claim["value"], int)
                    and not isinstance(claim["value"], bool)
                ),
                None,
            )
            if numeric is not None:
                numeric["value"] = float(numeric["value"])
        if case_id == self._rejected_candidate_case:
            rejected = next(
                source.claims[index]
                for source_case_id in case.source_claim_case_ids
                for source in (self._claim_cases[source_case_id],)
                for index in range(len(source.claims))
                if index not in source.expected["acceptedClaimIndexes"]
            )
            model_claims.append(materialize(rejected))
        if case_id == self._additional_accepted_claim_case:
            target = next(item for item in entities if item.relationship == "target")
            target_evidence = next(
                item
                for item in evidence
                if item.evidence_id in target.evidence_ids
                and target.label in item.content
            )
            model_claims.append(
                {
                    "evidenceId": target_evidence.evidence_id,
                    "subjectEntityId": target.entity_id,
                    "predicate": "identity",
                    "value": target.label,
                    "evidenceText": target.label,
                    "actorRole": target_evidence.actor.role.value,
                    "polarity": "positive",
                    "modality": "asserted",
                    "confidence": 0.99,
                    "unit": None,
                    "effectiveAt": None,
                    "supersedesClaimId": None,
                }
            )
        reviews = [
            {
                "evidenceId": evidence[item.evidence_index].evidence_id,
                "reason": item.category,
            }
            for item in case.expected_reviews
        ]
        if case_id == self._unexpected_review_case:
            reviews.append(
                {
                    "evidenceId": evidence[0].evidence_id,
                    "reason": "insufficient_evidence",
                }
            )
        if case_id == self._invalid_review_category_case and reviews:
            reviews[0]["reason"] = "private free-form reason"
        if case_id == self._wrong_review_evidence_case and reviews:
            reviews[0]["evidenceId"] = next(
                item.evidence_id
                for item in evidence
                if item.evidence_id != reviews[0]["evidenceId"]
            )
        usage = ProposalUsage(
            input_tokens=10,
            output_tokens=5,
            latency_ms=7,
            cost_microusd=3,
            provider_calls=1,
            provider_billed=True,
            usage_complete=True,
        )
        self._telemetry.record(usage)
        return ProposalResponse(
            model_output={"claims": model_claims, "review": reviews},
            usage=usage,
        )


class ReplayExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.claim_catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
        cls.interpretation_catalog = load_interpretation_fixture_catalog(
            INTERPRETATION_FIXTURE_PATH
        )
        cls.provider_quality_catalog = load_provider_quality_fixture_catalog(
            PROVIDER_QUALITY_FIXTURE_PATH,
            claim_catalog=cls.claim_catalog,
            interpretation_catalog=cls.interpretation_catalog,
        )

    def _adapter(self, **kwargs):
        return _AdapterWrapper(
            RecordedProposalAdapter(self.claim_catalog),
            **kwargs,
        )

    def _identity(
        self,
        adapter,
        *,
        repeats=3,
        source_tree_dirty=False,
        evaluation_profile="candidate_validation",
        evaluation_fixture_hash=None,
        case_count=None,
    ):
        if evaluation_fixture_hash is None:
            evaluation_fixture_hash = self.claim_catalog.manifest_hash
        if case_count is None:
            case_count = len(self.claim_catalog.cases)
        return ReplayIdentity.create(
            code_revision="a" * 40,
            source_tree_hash="f" * 64,
            source_tree_dirty=source_tree_dirty,
            python_version="3.12.11",
            dependency_lock_hash="b" * 64,
            interpretation_fixture_hash=self.interpretation_catalog.manifest_hash,
            claim_fixture_hash=self.claim_catalog.manifest_hash,
            evaluation_fixture_hash=evaluation_fixture_hash,
            extraction_schema_version=CLAIM_EXTRACTION_SCHEMA_VERSION,
            provider_id=adapter.provider_id,
            model_id=adapter.model_id,
            prompt_id=adapter.prompt_id,
            prompt_hash=adapter.prompt_hash,
            evaluation_profile=evaluation_profile,
            repeats=repeats,
            case_count=case_count,
            interpretation_case_count=len(self.interpretation_catalog.cases),
        )

    def test_full_recorded_corpus_repeats_exactly_with_one_call_and_usage_math(self):
        adapter = self._adapter()
        identity = self._identity(adapter)

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=identity,
        )

        self.assertTrue(report.passed)
        self.assertEqual(identity.planned_calls, adapter.calls)
        self.assertEqual(identity.planned_calls, len(report.results))
        self.assertEqual(
            identity.planned_interpretations,
            len(report.interpretation_results),
        )
        self.assertTrue(all(item.passed for item in report.interpretation_results))
        self.assertEqual((), report.interpretation_variance_case_ids)
        self.assertTrue(all(item.passed for item in report.results))
        self.assertEqual(0, report.input_tokens)
        self.assertEqual(0, report.output_tokens)
        self.assertEqual(0, report.latency_ms)
        self.assertEqual(0, report.cost_microusd)
        self.assertEqual(0, report.provider_calls)
        self.assertTrue(report.usage_complete)
        self.assertEqual((), report.proposal_variance_case_ids)
        self.assertEqual((), report.outcome_variance_case_ids)
        self.assertEqual(0, report.error_count)

    def test_adapter_failure_is_visible_without_exception_or_evidence_leakage(self):
        case_id = self.claim_catalog.cases[0].case_id
        adapter = self._adapter(fail_case=case_id)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        serialized = json.dumps(report.to_dict(), sort_keys=True)
        self.assertFalse(report.passed)
        self.assertEqual(1, report.error_count)
        self.assertFalse(report.usage_complete)
        failed = next(item for item in report.results if item.case_id == case_id)
        self.assertEqual("adapter_RuntimeError", failed.error_code)
        self.assertNotIn("private-broker@example.com", serialized)
        self.assertNotIn("The property is", serialized)

    def test_repeat_variance_fails_the_gate_even_when_claim_is_still_valid(self):
        case_id = next(
            case.case_id for case in self.claim_catalog.cases if case.claims
        )
        adapter = self._adapter(vary_case=case_id)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=2),
        )

        self.assertFalse(report.passed)
        self.assertIn(case_id, report.proposal_variance_case_ids)
        self.assertIn(case_id, report.outcome_variance_case_ids)
        self.assertEqual(0, report.error_count)

    def test_invalid_adapter_response_is_a_safe_visible_failure(self):
        case_id = self.claim_catalog.cases[0].case_id
        adapter = self._adapter(invalid_response_case=case_id)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        serialized = json.dumps(report.to_dict(), sort_keys=True)
        self.assertFalse(report.passed)
        self.assertEqual(1, report.error_count)
        failed = next(item for item in report.results if item.case_id == case_id)
        self.assertEqual("adapter_invalid_response", failed.error_code)
        self.assertNotIn("broker@example.com", serialized)

    def test_semantically_wrong_recorded_claim_fails_expected_outcome(self):
        case_id = next(
            case.case_id for case in self.claim_catalog.cases if case.claims
        )
        adapter = self._adapter(wrong_case=case_id)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        self.assertFalse(report.passed)
        failed = next(item for item in report.results if item.case_id == case_id)
        self.assertFalse(failed.passed)
        self.assertEqual("", failed.error_code)

    def test_declared_provider_calls_and_billed_usage_are_reconciled(self):
        usage = ProposalUsage(
            input_tokens=101,
            output_tokens=29,
            latency_ms=17,
            cost_microusd=13,
            provider_calls=1,
            provider_billed=True,
            usage_complete=True,
        )
        telemetry = _MutableTelemetry()
        adapter = self._adapter(
            usage=usage,
            provider_id="test-provider",
            telemetry=telemetry,
        )
        identity = self._identity(adapter, repeats=1)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=identity,
            telemetry=telemetry,
        )

        self.assertTrue(report.passed)
        self.assertEqual(identity.planned_calls, report.provider_calls)
        self.assertEqual(101 * identity.planned_calls, report.input_tokens)
        self.assertEqual(29 * identity.planned_calls, report.output_tokens)
        self.assertEqual(17 * identity.planned_calls, report.latency_ms)
        self.assertEqual(13 * identity.planned_calls, report.cost_microusd)
        self.assertEqual(identity.planned_calls, report.provider_billed_calls)
        self.assertTrue(report.usage_complete)

    def test_nonrecorded_adapter_without_complete_one_call_usage_fails_gate(self):
        adapter = self._adapter(provider_id="test-provider")
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        self.assertFalse(report.passed)
        self.assertEqual(0, report.provider_calls)

    def test_nonrecorded_adapter_requires_independent_telemetry(self):
        usage = ProposalUsage(
            input_tokens=1,
            output_tokens=1,
            latency_ms=1,
            cost_microusd=1,
            provider_calls=1,
            provider_billed=True,
            usage_complete=True,
        )
        adapter = self._adapter(usage=usage, provider_id="test-provider")

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        self.assertFalse(report.passed)
        self.assertEqual(0, report.provider_calls)

    def test_telemetry_undercount_and_usage_mismatch_fail_closed(self):
        declared = ProposalUsage(
            input_tokens=10,
            output_tokens=5,
            latency_ms=7,
            cost_microusd=3,
            provider_calls=1,
            provider_billed=True,
            usage_complete=True,
        )
        observed = ProposalUsage(
            provider_calls=1,
            provider_billed=False,
            usage_complete=True,
        )
        telemetry = _MutableTelemetry()
        adapter = self._adapter(
            usage=declared,
            provider_id="test-provider",
            telemetry=telemetry,
            observed_usage=observed,
            observed_attempts=0,
        )

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
            telemetry=telemetry,
        )

        self.assertFalse(report.passed)
        self.assertTrue(
            any(item.error_code == "transport_attempt_count_mismatch" for item in report.results)
        )

    def test_provider_exception_retains_observed_incomplete_attempt(self):
        case_id = self.claim_catalog.cases[0].case_id
        telemetry = _MutableTelemetry()
        adapter = self._adapter(
            provider_id="test-provider",
            fail_case=case_id,
            telemetry=telemetry,
        )

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
            telemetry=telemetry,
        )

        failed = next(item for item in report.results if item.case_id == case_id)
        self.assertEqual(1, failed.usage.provider_calls)
        self.assertFalse(failed.usage.usage_complete)
        self.assertFalse(report.passed)

    def test_provider_quality_uses_one_complete_case_per_unique_request(self):
        telemetry = _MutableTelemetry()
        adapter = _ProviderQualityRecordedAdapter(
            self.provider_quality_catalog,
            self.claim_catalog,
            telemetry,
        )
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            provider_quality_catalog=self.provider_quality_catalog,
            adapter=adapter,
            identity=self._identity(
                adapter,
                repeats=1,
                evaluation_profile="provider_quality",
                evaluation_fixture_hash=self.provider_quality_catalog.manifest_hash,
                case_count=len(self.provider_quality_catalog.cases),
            ),
            telemetry=telemetry,
        )

        self.assertTrue(report.passed)
        self.assertEqual(19, adapter.calls)
        self.assertEqual(19, len(report.results))
        self.assertEqual(
            {case.case_id for case in self.interpretation_catalog.cases},
            {item.case_id for item in report.results},
        )
        self.assertTrue(all(not item.quality_mismatch_codes for item in report.results))
        complete = next(
            item for item in report.results if item.case_id == "complete-property-facts"
        )
        self.assertEqual(13, sum(dict(complete.accepted_predicate_counts).values()))

    def test_provider_quality_reports_only_safe_mismatch_categories(self):
        scenarios = (
            (
                {"missing_claim_case": "fresh-reply-over-quoted-stale-history"},
                "missing_expected_claims",
            ),
            (
                {"unexpected_review_case": "fresh-reply-over-quoted-stale-history"},
                "unexpected_reviews",
            ),
            (
                {"invalid_review_category_case": "ambiguous-other-building"},
                "invalid_review_category",
            ),
            (
                {"wrong_review_evidence_case": "ambiguous-other-building"},
                "review_binding_mismatch",
            ),
            (
                {"detail_mismatch_case": "complete-property-facts"},
                "claim_detail_mismatch",
            ),
        )

        for adapter_options, expected_code in scenarios:
            with self.subTest(expected_code=expected_code):
                telemetry = _MutableTelemetry()
                adapter = _ProviderQualityRecordedAdapter(
                    self.provider_quality_catalog,
                    self.claim_catalog,
                    telemetry,
                    **adapter_options,
                )
                report = run_claim_replay(
                    interpretation_catalog=self.interpretation_catalog,
                    claim_catalog=self.claim_catalog,
                    provider_quality_catalog=self.provider_quality_catalog,
                    adapter=adapter,
                    identity=self._identity(
                        adapter,
                        repeats=1,
                        evaluation_profile="provider_quality",
                        evaluation_fixture_hash=(
                            self.provider_quality_catalog.manifest_hash
                        ),
                        case_count=len(self.provider_quality_catalog.cases),
                    ),
                    telemetry=telemetry,
                )

                self.assertFalse(report.passed)
                self.assertIn(
                    expected_code,
                    {
                        code
                        for item in report.results
                        for code in item.quality_mismatch_codes
                    },
                )

                if expected_code == "claim_detail_mismatch":
                    failed = next(
                        item
                        for item in report.results
                        if item.case_id == "complete-property-facts"
                    )
                    self.assertEqual(
                        (("effectiveAt", 1),),
                        failed.claim_mismatch_field_counts,
                    )
                    self.assertEqual(
                        {"effectiveAt": 1},
                        failed.to_dict()["claimMismatchFieldCounts"],
                    )
                serialized = json.dumps(report.to_dict(), sort_keys=True)
                self.assertNotIn("private free-form reason", serialized)

    def test_provider_quality_retains_safe_rejection_diagnostics_without_failing(self):
        telemetry = _MutableTelemetry()
        adapter = _ProviderQualityRecordedAdapter(
            self.provider_quality_catalog,
            self.claim_catalog,
            telemetry,
            rejected_candidate_case="ordinary-prose-does-not-fabricate-entities",
        )

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            provider_quality_catalog=self.provider_quality_catalog,
            adapter=adapter,
            identity=self._identity(
                adapter,
                repeats=1,
                evaluation_profile="provider_quality",
                evaluation_fixture_hash=self.provider_quality_catalog.manifest_hash,
                case_count=len(self.provider_quality_catalog.cases),
            ),
            telemetry=telemetry,
        )

        self.assertTrue(report.passed)
        result = next(
            item
            for item in report.results
            if item.case_id == "ordinary-prose-does-not-fabricate-entities"
        )
        self.assertEqual(
            (("predicate_evidence_mismatch:availability", 1),),
            result.rejected_predicate_counts,
        )
        self.assertEqual((), result.quality_mismatch_codes)

    def test_provider_quality_accepts_valid_quote_and_confidence_variants(self):
        telemetry = _MutableTelemetry()
        adapter = _ProviderQualityRecordedAdapter(
            self.provider_quality_catalog,
            self.claim_catalog,
            telemetry,
            nonsemantic_variation_case="complete-property-facts",
        )

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            provider_quality_catalog=self.provider_quality_catalog,
            adapter=adapter,
            identity=self._identity(
                adapter,
                repeats=1,
                evaluation_profile="provider_quality",
                evaluation_fixture_hash=self.provider_quality_catalog.manifest_hash,
                case_count=len(self.provider_quality_catalog.cases),
            ),
            telemetry=telemetry,
        )

        self.assertTrue(report.passed)
        complete = next(
            item
            for item in report.results
            if item.case_id == "complete-property-facts"
        )
        self.assertEqual((), complete.quality_mismatch_codes)
        self.assertEqual((), complete.claim_mismatch_field_counts)

    def test_provider_quality_allows_additional_validator_accepted_claims(self):
        telemetry = _MutableTelemetry()
        adapter = _ProviderQualityRecordedAdapter(
            self.provider_quality_catalog,
            self.claim_catalog,
            telemetry,
            additional_accepted_claim_case="complete-property-facts",
        )

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            provider_quality_catalog=self.provider_quality_catalog,
            adapter=adapter,
            identity=self._identity(
                adapter,
                repeats=1,
                evaluation_profile="provider_quality",
                evaluation_fixture_hash=self.provider_quality_catalog.manifest_hash,
                case_count=len(self.provider_quality_catalog.cases),
            ),
            telemetry=telemetry,
        )

        self.assertTrue(report.passed)
        complete = next(
            item
            for item in report.results
            if item.case_id == "complete-property-facts"
        )
        self.assertEqual(14, complete.accepted_claim_count)
        self.assertEqual((), complete.quality_mismatch_codes)

    def test_dirty_source_can_pass_evaluation_but_not_reproducible_gate(self):
        adapter = self._adapter()
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1, source_tree_dirty=True),
        )

        self.assertTrue(report.evaluation_passed)
        self.assertFalse(report.passed)

    def test_changed_claim_confidence_fails_complete_expected_claim_oracle(self):
        case_id = next(
            case.case_id for case in self.claim_catalog.cases if case.claims
        )
        adapter = self._adapter(confidence_case=case_id)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        self.assertFalse(report.passed)
        failed = next(item for item in report.results if item.case_id == case_id)
        self.assertFalse(failed.passed)

    def test_fixture_claim_mutation_cannot_change_independent_expected_oracle(self):
        original = next(
            case
            for case in self.claim_catalog.cases
            if case.case_id == "complete-property-facts-accepted"
        )
        asking_index = next(
            index
            for index, claim in enumerate(original.claims)
            if claim["predicate"] == "asking_status"
        )
        claims = list(original.claims)
        changed = dict(claims[asking_index])
        changed["value"] = "asking"
        claims[asking_index] = changed
        dishonest = replace(original, claims=tuple(claims))
        catalog = replace(
            self.claim_catalog,
            cases=tuple(
                dishonest if case.case_id == original.case_id else case
                for case in self.claim_catalog.cases
            ),
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        failed = next(item for item in report.results if item.case_id == original.case_id)
        self.assertFalse(failed.passed)
        self.assertFalse(report.passed)

    def test_same_issue_code_on_wrong_evidence_fails_complete_issue_oracle(self):
        case_id = "explicit-model-review-output"
        adapter = self._adapter(wrong_review_evidence_case=case_id)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        self.assertFalse(report.passed)
        failed = next(item for item in report.results if item.case_id == case_id)
        self.assertFalse(failed.passed)

    def test_identity_mismatch_is_rejected_before_an_adapter_call(self):
        adapter = self._adapter()
        identity = ReplayIdentity.create(
            code_revision="a" * 40,
            source_tree_hash="f" * 64,
            source_tree_dirty=True,
            python_version="3.12.11",
            dependency_lock_hash="b" * 64,
            interpretation_fixture_hash=self.interpretation_catalog.manifest_hash,
            claim_fixture_hash="f" * 64,
            evaluation_fixture_hash=self.claim_catalog.manifest_hash,
            extraction_schema_version=CLAIM_EXTRACTION_SCHEMA_VERSION,
            provider_id=adapter.provider_id,
            model_id=adapter.model_id,
            prompt_id=adapter.prompt_id,
            prompt_hash=adapter.prompt_hash,
            repeats=1,
            case_count=len(self.claim_catalog.cases),
            interpretation_case_count=len(self.interpretation_catalog.cases),
        )

        with self.assertRaises(ValueError):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=self.claim_catalog,
                adapter=adapter,
                identity=identity,
            )
        self.assertEqual(0, adapter.calls)

    def test_evaluation_fixture_hash_mismatch_is_rejected_before_adapter_call(self):
        adapter = self._adapter()

        with self.assertRaisesRegex(ValueError, "evaluation fixture hash"):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=self.claim_catalog,
                adapter=adapter,
                identity=self._identity(
                    adapter,
                    repeats=1,
                    evaluation_fixture_hash="0" * 64,
                ),
            )
        self.assertEqual(0, adapter.calls)

    def test_provider_profile_requires_explicit_quality_catalog(self):
        telemetry = _MutableTelemetry()
        adapter = _ProviderQualityRecordedAdapter(
            self.provider_quality_catalog,
            self.claim_catalog,
            telemetry,
        )

        with self.assertRaisesRegex(ValueError, "requires its expectation catalog"):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=self.claim_catalog,
                adapter=adapter,
                identity=self._identity(
                    adapter,
                    repeats=1,
                    evaluation_profile="provider_quality",
                    evaluation_fixture_hash=(
                        self.provider_quality_catalog.manifest_hash
                    ),
                    case_count=len(self.provider_quality_catalog.cases),
                ),
                telemetry=telemetry,
            )
        self.assertEqual(0, adapter.calls)

    def test_incident_dimension_labels_must_match_the_replayed_message_shape(self):
        original = self.claim_catalog.cases[0]
        dishonest = replace(
            original,
            incident_dimensions=tuple(
                sorted(set(original.incident_dimensions) | {"attachment"})
            ),
        )
        catalog = replace(
            self.claim_catalog,
            cases=(dishonest,) + self.claim_catalog.cases[1:],
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        with self.assertRaisesRegex(
            ValueError,
            "attachment coverage requires attachment evidence",
        ):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=catalog,
                adapter=adapter,
                identity=self._identity(adapter, repeats=1),
            )
        self.assertEqual(0, adapter.calls)

    def test_external_incident_dimension_must_bind_to_replayed_evidence(self):
        original = next(
            case
            for case in self.claim_catalog.cases
            if case.case_id == "alternate-attachment-cannot-identify-target"
        )
        body_bound_claim = dict(original.claims[0])
        body_bound_claim["evidenceIndex"] = 1
        dishonest = replace(original, claims=(body_bound_claim,))
        catalog = replace(
            self.claim_catalog,
            cases=tuple(
                dishonest if case.case_id == original.case_id else case
                for case in self.claim_catalog.cases
            ),
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        with self.assertRaisesRegex(
            ValueError,
            "attachment coverage requires a claim or review bound to attachment evidence",
        ):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=catalog,
                adapter=adapter,
                identity=self._identity(adapter, repeats=1),
            )
        self.assertEqual(0, adapter.calls)

    def test_terminal_closeout_dimension_requires_target_unavailability(self):
        original = next(
            case
            for case in self.claim_catalog.cases
            if case.case_id == "target-terminal-signals-accepted"
        )
        non_target_availability = dict(original.claims[0])
        non_target_subject = dict(non_target_availability["subject"])
        non_target_subject["relationship"] = "alternate"
        non_target_availability["subject"] = non_target_subject
        dishonest = replace(
            original,
            claims=(non_target_availability,) + original.claims[1:],
        )
        catalog = replace(
            self.claim_catalog,
            cases=tuple(
                dishonest if case.case_id == original.case_id else case
                for case in self.claim_catalog.cases
            ),
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        with self.assertRaisesRegex(
            ValueError,
            "terminal_closeout coverage requires target unavailability",
        ):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=catalog,
                adapter=adapter,
                identity=self._identity(adapter, repeats=1),
            )
        self.assertEqual(0, adapter.calls)

    def test_structured_prior_claim_values_replay_without_hashing_claim_objects(self):
        original = next(
            case
            for case in self.claim_catalog.cases
            if case.case_id == "repeated-information-request-accepted"
        )
        structured_prior = dict(original.prior_claims[0])
        structured_prior.update(
            {
                "predicate": "identity",
                "value": {"address": "123 Industrial Ave"},
                "evidenceText": "123 Industrial Ave",
            }
        )
        updated = replace(
            original,
            prior_claims=(original.prior_claims[0], structured_prior),
        )
        catalog = replace(
            self.claim_catalog,
            cases=tuple(
                updated if case.case_id == original.case_id else case
                for case in self.claim_catalog.cases
            ),
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        self.assertTrue(report.passed)

    def test_prior_history_must_be_evidence_bound_semantic_and_chronological(self):
        original = next(
            case
            for case in self.claim_catalog.cases
            if case.case_id == "broker-correction-accepted"
        )
        mutations = (
            ({"evidenceText": "unrelated prior assertion"}, "excerpt is absent"),
            ({"value": 13.0}, "predicate does not match its evidence"),
            ({"observedAt": "tomorrow"}, "chronology is invalid"),
            ({"observedAt": "2026-07-23T09:30:00Z"}, "must precede"),
            (
                {"observedAt": "2026-07-20T01:02:03Z"},
                "chronology is not evidence-bound",
            ),
            (
                {
                    "subject": {
                        "relationship": "contact",
                        "suite": "",
                        "canonicalAddress": "alex@example.com",
                    }
                },
                "subject is not evidence-bound",
            ),
        )

        for changes, message in mutations:
            with self.subTest(changes=changes):
                prior = dict(original.prior_claims[0])
                prior.update(changes)
                updated = replace(original, prior_claims=(prior,))
                catalog = replace(
                    self.claim_catalog,
                    cases=tuple(
                        updated if case.case_id == original.case_id else case
                        for case in self.claim_catalog.cases
                    ),
                )
                adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

                with self.assertRaisesRegex(ValueError, message):
                    run_claim_replay(
                        interpretation_catalog=self.interpretation_catalog,
                        claim_catalog=catalog,
                        adapter=adapter,
                        identity=self._identity(adapter, repeats=1),
                    )
                self.assertEqual(0, adapter.calls)

    def test_symbolic_supersession_selects_exact_prior_after_request_ordering(self):
        original = next(
            case
            for case in self.claim_catalog.cases
            if case.case_id == "broker-correction-accepted"
        )
        alternate = dict(original.prior_claims[0])
        alternate["evidenceText"] = "asking rent is $14.00/SF/yr"
        claims = []
        for raw in original.claims:
            claim = dict(raw)
            claim["supersedesClaimId"] = "prior:1"
            claims.append(claim)
        updated = replace(
            original,
            prior_claims=(alternate, original.prior_claims[0]),
            claims=tuple(claims),
        )
        catalog = replace(
            self.claim_catalog,
            cases=tuple(
                updated if case.case_id == original.case_id else case
                for case in self.claim_catalog.cases
            ),
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=catalog,
            adapter=adapter,
            identity=self._identity(adapter, repeats=1),
        )

        request = adapter.requests_by_case[original.case_id]
        selected_id = next(
            item.claim_id
            for item in request.prior_claims
            if item.evidence_text == original.prior_claims[0]["evidenceText"]
        )
        self.assertTrue(
            all(
                claim["supersedesClaimId"] == selected_id
                for claim in adapter.responses_by_case[original.case_id].model_output[
                    "claims"
                ]
            )
        )
        self.assertTrue(report.passed)

    def test_requirements_mismatch_dimension_requires_fit_only_evidence(self):
        original = next(
            case
            for case in self.claim_catalog.cases
            if case.case_id == "complete-property-facts-rejected-near-misses"
        )
        dishonest = replace(
            original,
            incident_dimensions=tuple(
                sorted(set(original.incident_dimensions) | {"requirements_mismatch"})
            ),
        )
        catalog = replace(
            self.claim_catalog,
            cases=tuple(
                dishonest if case.case_id == original.case_id else case
                for case in self.claim_catalog.cases
            ),
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        with self.assertRaisesRegex(
            ValueError,
            "requirements_mismatch coverage requires fit-only availability evidence",
        ):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=catalog,
                adapter=adapter,
                identity=self._identity(adapter, repeats=1),
            )
        self.assertEqual(0, adapter.calls)

    def test_in_memory_private_case_identifier_is_rejected_before_replay(self):
        original = self.claim_catalog.cases[0]
        private = replace(original, case_id="private-broker@example.com")
        catalog = replace(
            self.claim_catalog,
            cases=(private,) + self.claim_catalog.cases[1:],
        )
        adapter = _AdapterWrapper(RecordedProposalAdapter(catalog))

        with self.assertRaisesRegex(ValueError, "case_id must be a report-safe identifier"):
            run_claim_replay(
                interpretation_catalog=self.interpretation_catalog,
                claim_catalog=catalog,
                adapter=adapter,
                identity=self._identity(adapter, repeats=1),
            )
        self.assertEqual(0, adapter.calls)


class ReplayCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "claim_replay_cli_under_test", REPLAY_SCRIPT
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load replay CLI module")
        cls.cli_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli_module)

    def _run(self, *arguments):
        return subprocess.run(
            [sys.executable, str(REPLAY_SCRIPT), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run_with_env(self, *arguments, env):
        return subprocess.run(
            [sys.executable, str(REPLAY_SCRIPT), *arguments],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def test_cli_stamps_identity_and_runs_three_repeat_corpus_without_pii(self):
        completed = self._run("--repeats", "3")

        self.assertIn(completed.returncode, (0, 1), completed.stderr)
        payload = json.loads(completed.stdout)
        current_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertTrue(payload["evaluationPassed"])
        self.assertEqual(
            not payload["identity"]["sourceTreeDirty"], payload["passed"]
        )
        self.assertEqual(0 if payload["passed"] else 1, completed.returncode)
        self.assertEqual(current_sha, payload["identity"]["codeRevision"])
        self.assertRegex(payload["identity"]["sourceTreeHash"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(payload["identity"]["sourceTreeDirty"], bool)
        self.assertEqual(29, payload["identity"]["caseCount"])
        self.assertEqual(19, payload["identity"]["interpretationCaseCount"])
        self.assertEqual(87, payload["identity"]["plannedCalls"])
        self.assertEqual(57, payload["identity"]["plannedInterpretations"])
        self.assertEqual(87, payload["summary"]["resultCount"])
        self.assertEqual(57, payload["summary"]["interpretationResultCount"])
        self.assertEqual(0, payload["summary"]["providerBilledCalls"])
        self.assertEqual(0, payload["summary"]["totalTokens"])
        self.assertEqual(0, payload["summary"]["costMicrousd"])
        self.assertNotIn("@", completed.stdout)
        self.assertNotIn("property is available", completed.stdout.lower())

    def test_output_is_opt_in_and_matches_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            completed = self._run(
                "--repeats",
                "1",
                "--output",
                str(output_path),
            )

            self.assertIn(completed.returncode, (0, 1), completed.stderr)
            self.assertTrue(output_path.exists())
            self.assertEqual(
                json.loads(completed.stdout),
                json.loads(output_path.read_text(encoding="utf-8")),
            )

    def test_invalid_repeat_count_is_rejected_before_replay(self):
        completed = self._run("--repeats", "11")

        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)

    def test_provider_mode_requires_explicit_call_permission(self):
        completed = self._run("--provider", "openai")

        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn("--allow-provider-calls", completed.stderr)

    def test_provider_mode_requires_key_before_transport_construction(self):
        environment = dict(os.environ)
        environment.pop("OPENAI_API_KEY", None)
        completed = self._run_with_env(
            "--provider",
            "openai",
            "--allow-provider-calls",
            "--repeats",
            "1",
            env=environment,
        )

        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn("OPENAI_API_KEY", completed.stderr)

    def test_provider_mode_caps_total_calls_at_eighty_four(self):
        environment = dict(os.environ)
        environment["OPENAI_API_KEY"] = "test-key"
        completed = self._run_with_env(
            "--provider",
            "openai",
            "--allow-provider-calls",
            "--repeats",
            "5",
            env=environment,
        )

        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn("84", completed.stderr)

    def test_source_hash_reads_only_the_bounded_replay_surface(self):
        paths = self.cli_module._replay_surface_paths()
        relative = {path.relative_to(REPO_ROOT).as_posix() for path in paths}

        self.assertIn("email_automation/claim_pipeline/replay.py", relative)
        self.assertIn("scripts/run_claim_pipeline_replay.py", relative)
        self.assertIn("scripts/claim_pipeline_openai_transport.py", relative)
        self.assertIn("requirements.lock", relative)
        self.assertIn(
            "tests/fixtures/claim_pipeline_interpretation_cases.json", relative
        )
        self.assertIn("tests/fixtures/claim_pipeline_claim_cases.json", relative)
        self.assertIn(
            "tests/fixtures/claim_pipeline_provider_quality_cases.json", relative
        )
        self.assertNotIn("service-account.json", relative)
        self.assertTrue(
            all(
                value.startswith("email_automation/claim_pipeline/")
                or value
                in {
                    "scripts/claim_pipeline_openai_transport.py",
                    "scripts/run_claim_pipeline_replay.py",
                    "requirements.lock",
                    "tests/fixtures/claim_pipeline_interpretation_cases.json",
                    "tests/fixtures/claim_pipeline_claim_cases.json",
                    "tests/fixtures/claim_pipeline_provider_quality_cases.json",
                }
                for value in relative
            )
        )


if __name__ == "__main__":
    unittest.main()
