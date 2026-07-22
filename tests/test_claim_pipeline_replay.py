import json
import importlib.util
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
from email_automation.claim_pipeline.replay import (
    MAX_REPLAY_CALLS,
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
            "extraction_schema_version": 1,
            "provider_id": "recorded",
            "model_id": "fixture-output-v1",
            "prompt_id": "recorded-claim-proposal-v1",
            "prompt_hash": "e" * 64,
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
                "extractionSchemaVersion",
                "providerId",
                "modelId",
                "promptId",
                "promptHash",
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
            {"dependency_lock_hash": "not-a-hash"},
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
        provider_id="",
    ):
        self._recorded = recorded
        self._usage = usage
        self._fail_case = fail_case
        self._vary_case = vary_case
        self._invalid_response_case = invalid_response_case
        self._wrong_case = wrong_case
        self._confidence_case = confidence_case
        self._wrong_review_evidence_case = wrong_review_evidence_case
        self._case_calls = {}
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
        if case_id == self._fail_case:
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
        return ProposalResponse(
            model_output=output,
            usage=self._usage or response.usage,
        )


class ReplayExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.claim_catalog = load_claim_fixture_catalog(CLAIM_FIXTURE_PATH)
        cls.interpretation_catalog = load_interpretation_fixture_catalog(
            INTERPRETATION_FIXTURE_PATH
        )

    def _adapter(self, **kwargs):
        return _AdapterWrapper(
            RecordedProposalAdapter(self.claim_catalog),
            **kwargs,
        )

    def _identity(self, adapter, *, repeats=3, source_tree_dirty=False):
        return ReplayIdentity.create(
            code_revision="a" * 40,
            source_tree_hash="f" * 64,
            source_tree_dirty=source_tree_dirty,
            python_version="3.12.11",
            dependency_lock_hash="b" * 64,
            interpretation_fixture_hash=self.interpretation_catalog.manifest_hash,
            claim_fixture_hash=self.claim_catalog.manifest_hash,
            extraction_schema_version=1,
            provider_id=adapter.provider_id,
            model_id=adapter.model_id,
            prompt_id=adapter.prompt_id,
            prompt_hash=adapter.prompt_hash,
            repeats=repeats,
            case_count=len(self.claim_catalog.cases),
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
        adapter = self._adapter(usage=usage, provider_id="test-provider")
        identity = self._identity(adapter, repeats=1)
        report = run_claim_replay(
            interpretation_catalog=self.interpretation_catalog,
            claim_catalog=self.claim_catalog,
            adapter=adapter,
            identity=identity,
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
            extraction_schema_version=1,
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
        self.assertEqual(20, payload["identity"]["caseCount"])
        self.assertEqual(14, payload["identity"]["interpretationCaseCount"])
        self.assertEqual(60, payload["identity"]["plannedCalls"])
        self.assertEqual(42, payload["identity"]["plannedInterpretations"])
        self.assertEqual(60, payload["summary"]["resultCount"])
        self.assertEqual(42, payload["summary"]["interpretationResultCount"])
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

    def test_source_hash_reads_only_the_bounded_replay_surface(self):
        paths = self.cli_module._replay_surface_paths()
        relative = {path.relative_to(REPO_ROOT).as_posix() for path in paths}

        self.assertIn("email_automation/claim_pipeline/replay.py", relative)
        self.assertIn("scripts/run_claim_pipeline_replay.py", relative)
        self.assertIn("requirements.lock", relative)
        self.assertIn(
            "tests/fixtures/claim_pipeline_interpretation_cases.json", relative
        )
        self.assertIn("tests/fixtures/claim_pipeline_claim_cases.json", relative)
        self.assertNotIn("service-account.json", relative)
        self.assertTrue(
            all(
                value.startswith("email_automation/claim_pipeline/")
                or value
                in {
                    "scripts/run_claim_pipeline_replay.py",
                    "requirements.lock",
                    "tests/fixtures/claim_pipeline_interpretation_cases.json",
                    "tests/fixtures/claim_pipeline_claim_cases.json",
                }
                for value in relative
            )
        )


if __name__ == "__main__":
    unittest.main()
