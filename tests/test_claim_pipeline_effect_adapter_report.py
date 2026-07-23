import contextlib
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
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
EXPECTED_RESULTS = 54
EXPECTED_FIXTURE_HASH = (
    "c654da2a9a2fadee2f8cce761e17dd78d21168cfcb6b6e9fd06aeddff69ea229"
)
EXPECTED_SCHEMA_VERSION = "claim-pipeline-effect-adapter-fixtures-v1"
EXPECTED_CASE_IDS = (
    "automatic-fact-matching",
    "automatic-fact-stale-prior",
    "whole-plan-stale-snapshot",
    "whole-plan-stale-contract",
    "already-committed-effect",
    "human-action-no-approval",
    "human-action-exact-approval",
    "approval-for-other-action",
    "approval-wrong-plan",
    "forbidden-plan",
    "unsupported-actions",
    "terminal-outbound-draft",
    "terminal-followup-freeze",
    "dependency-chain-eligible",
    "dependency-chain-blocked",
    "dependency-construction-rejected",
    "scope-and-provenance-rejected",
    "input-order-byte-stable",
)
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

    def _run(
        self,
        output_path,
        *,
        fixture_path=FIXTURE_PATH,
        runs=EXPECTED_RUNS,
    ):
        with mock.patch.object(
            self.script,
            "_source_tree_dirty",
            return_value=False,
        ):
            return self.script.run_dry_run(
                fixture_path=fixture_path,
                runs=runs,
                output_path=output_path,
            )

    def _seed_passed_report(self, path):
        path.write_text('{"passed":true}\n', encoding="utf-8")

    def _seed_atomic_temporary(self, output_path, suffix="stale"):
        path = output_path.parent / f".{output_path.name}.{suffix}"
        path.write_text('{"passed":true}\n', encoding="utf-8")
        return path

    def _run_cli_subprocess(self, *arguments):
        return subprocess.run(
            (sys.executable, str(SCRIPT_PATH), *map(str, arguments)),
            cwd=REPO_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _fixture_payload(self):
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _write_fixture(self, directory, payload, name="fixture.json"):
        path = Path(directory) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_runner_constants_pin_the_canonical_committed_oracle(self):
        self.assertEqual(
            FIXTURE_PATH.resolve(),
            getattr(self.script, "CANONICAL_FIXTURE_PATH", None),
        )
        self.assertEqual(
            EXPECTED_FIXTURE_HASH,
            getattr(self.script, "CANONICAL_FIXTURE_SHA256", None),
        )
        self.assertEqual(
            EXPECTED_SCHEMA_VERSION,
            getattr(self.script, "CANONICAL_FIXTURE_SCHEMA_VERSION", None),
        )
        self.assertEqual(
            EXPECTED_CASE_IDS,
            getattr(self.script, "CANONICAL_CASE_IDS", None),
        )
        self.assertEqual(
            EXPECTED_RESULTS,
            getattr(self.script, "CANONICAL_RESULT_COUNT", None),
        )

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
        self.assertEqual(EXPECTED_RESULTS, report["summary"]["resultCount"])
        self.assertEqual(EXPECTED_RESULTS, report["summary"]["passedResultCount"])
        self.assertEqual([], report["summary"]["varianceCaseIds"])
        self.assertEqual(
            EXPECTED_STATUS_COUNTS,
            report["summary"]["statusCounts"],
        )
        self.assertEqual(
            EXPECTED_REASON_COUNTS,
            report["summary"]["reasonCounts"],
        )
        self.assertEqual(EXPECTED_RESULTS, len(report["results"]))
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

    def test_report_can_only_emit_canonical_case_ids_and_approved_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self._run(Path(directory) / "report.json")

        self.assertEqual(
            EXPECTED_CASE_IDS * EXPECTED_RUNS,
            tuple(item["caseId"] for item in report["results"]),
        )
        self.assertEqual(
            {"identity", "summary", "results", "passed", "resultDigest"},
            set(report),
        )
        for item in report["results"]:
            self.assertIn(item["caseId"], EXPECTED_CASE_IDS)
            self.assertEqual(
                {
                    "caseId",
                    "passed",
                    "statusReasonSignatures",
                    "receiptDigest",
                },
                set(item),
            )

        tainted = json.loads(json.dumps(report))
        tainted["results"][0]["benignExtraField"] = "opaque"
        with self.assertRaisesRegex(
            self.script.DryRunReportError,
            "allowlist",
        ):
            self.script._assert_private_report(tainted)

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

    def test_external_one_case_fixture_is_rejected_and_stale_output_removed(self):
        payload = self._fixture_payload()
        payload["cases"] = payload["cases"][:1]
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = self._write_fixture(directory, payload)
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)

            with self.assertRaisesRegex(
                self.script.DryRunReportError,
                "canonical fixture path",
            ):
                self._run(output_path, fixture_path=fixture_path)

            self.assertFalse(output_path.exists())

    def test_exact_fixture_copy_at_wrong_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "copy.json"
            fixture_path.write_bytes(FIXTURE_PATH.read_bytes())
            output_path = Path(directory) / "report.json"

            with self.assertRaisesRegex(
                self.script.DryRunReportError,
                "canonical fixture path",
            ):
                self._run(output_path, fixture_path=fixture_path)

            alias_path = Path(directory) / "alias.json"
            alias_path.symlink_to(fixture_path)
            with self.assertRaisesRegex(
                self.script.DryRunReportError,
                "canonical fixture path",
            ):
                self._run(output_path, fixture_path=alias_path)

    def test_modified_eighteen_case_fixture_is_rejected_by_hash_and_id_allowlist(self):
        payload = self._fixture_payload()
        payload["cases"][0]["caseId"] = "john-smith-123-main-street"
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = self._write_fixture(directory, payload)
            output_path = Path(directory) / "report.json"

            with mock.patch.object(
                self.script,
                "CANONICAL_FIXTURE_PATH",
                fixture_path.resolve(),
                create=True,
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "fixture hash",
            ):
                self._run(output_path, fixture_path=fixture_path)

            with mock.patch.object(
                self.script,
                "CANONICAL_FIXTURE_PATH",
                fixture_path.resolve(),
                create=True,
            ), mock.patch.object(
                self.script,
                "_file_hash",
                return_value=EXPECTED_FIXTURE_HASH,
            ), self.assertRaisesRegex(
                self.script.DryRunReportError,
                "canonical case IDs",
            ):
                self._run(output_path, fixture_path=fixture_path)

    def test_schema_and_case_count_are_checked_independently(self):
        catalog = self.script.load_effect_adapter_fixture_catalog(FIXTURE_PATH)
        invalid_catalogs = (
            (
                replace(catalog, schema_version="unexpected-schema"),
                "schema version",
            ),
            (
                replace(catalog, cases=catalog.cases[:1]),
                "exactly 18 cases",
            ),
        )
        for invalid_catalog, pattern in invalid_catalogs:
            with self.subTest(
                pattern=pattern
            ), tempfile.TemporaryDirectory() as directory:
                output_path = Path(directory) / "report.json"
                with mock.patch.object(
                    self.script,
                    "_source_tree_dirty",
                    return_value=False,
                ), mock.patch.object(
                    self.script,
                    "load_effect_adapter_fixture_catalog",
                    return_value=invalid_catalog,
                ), self.assertRaisesRegex(
                    self.script.DryRunReportError,
                    pattern,
                ):
                    self.script.run_dry_run(
                        fixture_path=FIXTURE_PATH,
                        runs=EXPECTED_RUNS,
                        output_path=output_path,
                    )

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
            ), mock.patch.object(
                self.script,
                "CANONICAL_FIXTURE_PATH",
                fixture_path.resolve(),
                create=True,
            ), mock.patch.object(
                self.script,
                "_file_hash",
                return_value=EXPECTED_FIXTURE_HASH,
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
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)
            with mock.patch.object(
                self.script,
                "load_effect_adapter_fixture_catalog",
            ) as loader, self.assertRaisesRegex(
                self.script.DryRunReportError,
                "exactly 3",
            ):
                self.script.run_dry_run(
                    fixture_path=FIXTURE_PATH,
                    runs=2,
                    output_path=output_path,
                )

            loader.assert_not_called()
            self.assertFalse(output_path.exists())

    def test_atomic_write_failures_leave_no_target_or_temporary_artifact(self):
        original_named_temporary_file = tempfile.NamedTemporaryFile

        class WriteFailingTemporaryFile:
            def __init__(self, *args, **kwargs):
                self._context = original_named_temporary_file(*args, **kwargs)
                self._stream = None

            def __enter__(self):
                self._stream = self._context.__enter__()
                return self

            def __exit__(self, *args):
                return self._context.__exit__(*args)

            @property
            def name(self):
                return self._stream.name

            def write(self, _content):
                raise OSError("injected temporary write failure")

        fault_patches = (
            mock.patch.object(
                self.script.tempfile,
                "NamedTemporaryFile",
                WriteFailingTemporaryFile,
            ),
            mock.patch.object(
                self.script.os,
                "replace",
                side_effect=OSError("injected replace failure"),
            ),
        )
        for fault_patch in fault_patches:
            with self.subTest(
                fault=fault_patch
            ), tempfile.TemporaryDirectory() as directory:
                output_path = Path(directory) / "report.json"
                self._seed_passed_report(output_path)
                with fault_patch, self.assertRaisesRegex(
                    self.script.DryRunReportError,
                    "could not be written",
                ):
                    self._run(output_path)

                self.assertFalse(output_path.exists())
                self.assertEqual(
                    [],
                    list(Path(directory).glob(f".{output_path.name}.*")),
                )

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

            for invalid_runs in ("2", "not-an-integer"):
                with self.subTest(runs=invalid_runs):
                    self._seed_passed_report(output_path)
                    with contextlib.redirect_stderr(
                        io.StringIO()
                    ), self.assertRaises(SystemExit):
                        self.script.main(
                            (
                                "--fixture",
                                str(FIXTURE_PATH),
                                "--runs",
                                invalid_runs,
                                "--output",
                                str(output_path),
                            )
                        )
                    self.assertFalse(output_path.exists())

    def test_cli_invalid_fixture_removes_seeded_passed_output(self):
        payload = self._fixture_payload()
        payload["cases"] = payload["cases"][:1]
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = self._write_fixture(directory, payload)
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)

            with mock.patch.object(
                self.script,
                "_source_tree_dirty",
                return_value=False,
            ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
                SystemExit,
            ):
                self.script.main(
                    (
                        "--fixture",
                        str(fixture_path),
                        "--runs",
                        "3",
                        "--output",
                        str(output_path),
                    )
                )

            self.assertFalse(output_path.exists())

    def test_subprocess_unknown_option_clears_stale_output_before_argparse(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            temporary_path = self._seed_atomic_temporary(output_path)
            self._seed_passed_report(output_path)

            completed = self._run_cli_subprocess(
                "--fixture",
                FIXTURE_PATH,
                "--runs",
                "3",
                "--output",
                output_path,
                "--unexpected-option",
            )

            self.assertEqual(2, completed.returncode)
            self.assertIn("unrecognized arguments", completed.stderr)
            self.assertFalse(output_path.exists())
            self.assertFalse(temporary_path.exists())

    def test_subprocess_missing_required_arg_clears_supplied_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            self._seed_passed_report(output_path)

            completed = self._run_cli_subprocess(
                "--runs",
                "3",
                "--output",
                output_path,
            )

            self.assertEqual(2, completed.returncode)
            self.assertIn("required", completed.stderr)
            self.assertFalse(output_path.exists())

    def test_subprocess_output_equals_variant_clears_before_parser_error(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "report.json"
            temporary_path = self._seed_atomic_temporary(output_path)
            self._seed_passed_report(output_path)

            completed = self._run_cli_subprocess(
                "--fixture",
                FIXTURE_PATH,
                "--runs",
                "3",
                f"--output={output_path}",
                "--unexpected-option",
            )

            self.assertEqual(2, completed.returncode)
            self.assertFalse(output_path.exists())
            self.assertFalse(temporary_path.exists())

    def test_subprocess_repeated_and_malformed_outputs_clear_every_candidate(self):
        cases = (
            ("repeated", lambda first, _second: ("--output", first, "--output", first)),
            (
                "conflicting",
                lambda first, second: (
                    "--output",
                    first,
                    f"--output={second}",
                ),
            ),
            (
                "malformed",
                lambda first, _second: (
                    "--output",
                    first,
                    "--output=",
                ),
            ),
        )
        for label, output_arguments in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                first_path = Path(directory) / "first.json"
                second_path = Path(directory) / "second.json"
                paths = (first_path,) if label != "conflicting" else (
                    first_path,
                    second_path,
                )
                temporary_paths = tuple(
                    self._seed_atomic_temporary(path) for path in paths
                )
                for path in paths:
                    self._seed_passed_report(path)

                completed = self._run_cli_subprocess(
                    "--fixture",
                    FIXTURE_PATH,
                    "--runs",
                    "3",
                    *output_arguments(first_path, second_path),
                )

                self.assertEqual(2, completed.returncode)
                self.assertIn("--output", completed.stderr)
                self.assertTrue(all(not path.exists() for path in paths))
                self.assertTrue(
                    all(not path.exists() for path in temporary_paths)
                )

    def test_subprocess_does_not_treat_unrelated_values_as_output_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            unrelated_path = Path(directory) / "unrelated.json"
            self._seed_passed_report(unrelated_path)

            completed = self._run_cli_subprocess(
                "--unexpected-option",
                unrelated_path,
            )

            self.assertEqual(2, completed.returncode)
            self.assertTrue(unrelated_path.exists())


if __name__ == "__main__":
    unittest.main()
