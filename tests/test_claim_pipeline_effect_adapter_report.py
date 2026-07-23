import contextlib
import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = (
    REPO_ROOT / "scripts" / "run_claim_pipeline_effect_adapter_dry_run.py"
)
FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "claim_pipeline_effect_adapter_cases.json"
)
EXPECTED_RUNS = 3
EXPECTED_CASES = 18
EXPECTED_STATUS_COUNTS = {
    "blocked": 48,
    "skipped": 9,
    "would_apply": 21,
}
EXPECTED_REASON_COUNTS = {
    "approval_required": 6,
    "approval_scope_mismatch": 3,
    "dependency_blocked": 3,
    "eligible_automatic_action": 18,
    "eligible_human_approved_action": 3,
    "idempotency_key_already_committed": 3,
    "plan_contract_violation": 12,
    "prior_state_mismatch": 6,
    "stale_contract": 3,
    "stale_snapshot": 3,
    "terminal_outbound_suppressed": 3,
    "unsupported_action_type": 15,
}
EMAIL_LIKE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "effect_adapter_dry_run_script",
        SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _canonical_digest(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EffectAdapterReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _load_script()

    def _run(self, output_path):
        with mock.patch.object(
            self.script,
            "_source_tree_dirty",
            return_value=False,
        ):
            return self.script.run_dry_run(
                fixture_path=FIXTURE_PATH,
                runs=EXPECTED_RUNS,
                output_path=output_path,
            )

    def _seed_passed_report(self, path):
        path.write_text('{"passed":true}\n', encoding="utf-8")

    def test_report_has_exact_identity_counts_results_and_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            report = self._run(output_path)

        self.assertTrue(report["passed"])
        self.assertEqual(
            {
                "profile",
                "codeRevision",
                "sourceTreeDirty",
                "sourceTreeHash",
                "fixtureHash",
                "caseCount",
                "runs",
            },
            set(report["identity"]),
        )
        self.assertEqual(
            "disabled-effect-adapter-dry-run-v1",
            report["identity"]["profile"],
        )
        self.assertEqual(40, len(report["identity"]["codeRevision"]))
        self.assertEqual(64, len(report["identity"]["sourceTreeHash"]))
        self.assertEqual(64, len(report["identity"]["fixtureHash"]))
        self.assertEqual(EXPECTED_RUNS, report["identity"]["runs"])
        self.assertEqual(EXPECTED_CASES, report["identity"]["caseCount"])
        self.assertFalse(report["identity"]["sourceTreeDirty"])
        self.assertEqual(
            {
                "resultCount",
                "passedResultCount",
                "varianceCaseIds",
                "statusCounts",
                "reasonCounts",
            },
            set(report["summary"]),
        )
        self.assertEqual(54, report["summary"]["resultCount"])
        self.assertEqual(54, report["summary"]["passedResultCount"])
        self.assertEqual([], report["summary"]["varianceCaseIds"])
        self.assertEqual(
            EXPECTED_STATUS_COUNTS,
            report["summary"]["statusCounts"],
        )
        self.assertEqual(
            EXPECTED_REASON_COUNTS,
            report["summary"]["reasonCounts"],
        )
        self.assertEqual(54, len(report["results"]))
        self.assertEqual(
            {
                "caseId",
                "passed",
                "statusReasonSignatures",
                "receiptDigest",
            },
            set(report["results"][0]),
        )
        unsigned = dict(report)
        result_digest = unsigned.pop("resultDigest")
        self.assertEqual(_canonical_digest(unsigned), result_digest)

    def test_encoded_report_is_private_and_contains_no_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self._run(Path(directory) / "report.json")

        encoded = json.dumps(report, sort_keys=True)
        self.assertNotRegex(encoded, EMAIL_LIKE)
        for forbidden in (
            "payload",
            "recipient",
            "evidenceText",
            "row-fixture",
            "timestamp",
            "createdAt",
            "completedAt",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_repeated_clean_executions_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.json"
            second_path = Path(directory) / "second.json"

            first_report = self._run(first_path)
            second_report = self._run(second_path)

            self.assertEqual(first_report, second_report)
            self.assertEqual(first_path.read_bytes(), second_path.read_bytes())

    def test_dirty_tree_removes_stale_passed_output_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)

            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=True,
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "clean committed source tree",
            ):
                self.script.run_dry_run(
                    fixture_path=FIXTURE_PATH,
                    runs=EXPECTED_RUNS,
                    output_path=output_path,
                )

            self.assertFalse(output_path.exists())

    def test_fixture_expectation_mismatch_stops_before_passed_report(self):
        catalog = self.script.load_effect_adapter_fixture_catalog(FIXTURE_PATH)
        original = self.script.run_effect_adapter_fixture_case

        def mismatch(case):
            result = original(case)
            if case.case_id == catalog.cases[0].case_id:
                return replace(result, passed=False)
            return result

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)
            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=False,
            ), mock.patch.object(
                self.script,
                "run_effect_adapter_fixture_case",
                side_effect=mismatch,
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "fixture expectation mismatch",
            ):
                self.script.run_dry_run(
                    fixture_path=FIXTURE_PATH,
                    runs=EXPECTED_RUNS,
                    output_path=output_path,
                )

            self.assertFalse(output_path.exists())

    def test_receipt_variance_stops_before_passed_report(self):
        original = self.script.run_effect_adapter_fixture_case
        invocation = 0

        def varying(case):
            nonlocal invocation
            invocation += 1
            result = original(case)
            if case.case_id == "automatic-fact-matching":
                return replace(result, receipt_id=f"receipt-variant-{invocation}")
            return result

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)
            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=False,
            ), mock.patch.object(
                self.script,
                "run_effect_adapter_fixture_case",
                side_effect=varying,
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "receipt variance",
            ):
                self.script.run_dry_run(
                    fixture_path=FIXTURE_PATH,
                    runs=EXPECTED_RUNS,
                    output_path=output_path,
                )

            self.assertFalse(output_path.exists())

    def test_incomplete_run_stops_before_passed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)
            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=False,
            ), mock.patch.object(
                self.script,
                "run_effect_adapter_fixture_case",
                side_effect=RuntimeError("private failure detail"),
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "fixture execution failed",
            ):
                self.script.run_dry_run(
                    fixture_path=FIXTURE_PATH,
                    runs=EXPECTED_RUNS,
                    output_path=output_path,
                )

            self.assertFalse(output_path.exists())

    def test_parse_failure_stops_before_passed_report(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "invalid.json"
            fixture_path.write_text("{", encoding="utf-8")
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)
            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=False,
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "fixture catalog rejected",
            ):
                self.script.run_dry_run(
                    fixture_path=fixture_path,
                    runs=EXPECTED_RUNS,
                    output_path=output_path,
                )

            self.assertFalse(output_path.exists())

    def test_privacy_failure_stops_before_passed_report(self):
        original = self.script.run_effect_adapter_fixture_case

        def tainted(case):
            result = original(case)
            if case.case_id != "automatic-fact-matching":
                return result
            receipt = dict(result.receipts[0])
            receipt["reason"] = "privacy-probe@" + "example.invalid"
            return replace(result, receipts=(receipt,))

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)
            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=False,
            ), mock.patch.object(
                self.script,
                "run_effect_adapter_fixture_case",
                side_effect=tainted,
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "privacy",
            ):
                self.script.run_dry_run(
                    fixture_path=FIXTURE_PATH,
                    runs=EXPECTED_RUNS,
                    output_path=output_path,
                )

            self.assertFalse(output_path.exists())

    def test_runs_other_than_three_are_rejected_before_fixture_execution(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            self.script,
            "load_effect_adapter_fixture_catalog",
        ) as loader, self.assertRaisesRegex(
            self.script.DryRunReportError,
            "exactly 3",
        ):
            self.script.run_dry_run(
                fixture_path=FIXTURE_PATH,
                runs=2,
                output_path=Path(directory) / "report.json",
            )

        loader.assert_not_called()

    def test_clean_tree_check_includes_untracked_files(self):
        with mock.patch.object(
            self.script,
            "_git",
            return_value=b"?? untracked-source.py\n",
        ) as git:
            self.assertTrue(self.script._source_tree_dirty())

        git.assert_called_once_with(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )

    def test_cli_writes_exact_report_and_rejects_other_run_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            stdout = io.StringIO()
            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=False,
            ), contextlib.redirect_stdout(stdout):
                status = self.script.main(
                    (
                        "--fixture",
                        str(FIXTURE_PATH),
                        "--runs",
                        "3",
                        "--output",
                        str(output_path),
                    )
                )

            self.assertEqual(0, status)
            self.assertEqual(output_path.read_text(encoding="utf-8"), stdout.getvalue())
            self.assertTrue(json.loads(stdout.getvalue())["passed"])

            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
                SystemExit,
            ):
                self.script.main(
                    (
                        "--fixture",
                        str(FIXTURE_PATH),
                        "--runs",
                        "2",
                        "--output",
                        str(output_path),
                    )
                )


if __name__ == "__main__":
    unittest.main()
