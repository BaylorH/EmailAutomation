import contextlib
import hashlib
import importlib.util
import inspect
import io
import json
import os
import re
import stat
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
CANONICAL_OUTPUT_PATH = Path(
    "/tmp/sitesift-disabled-effect-adapter-report.json"
)
OWNED_TEMP_PATH = Path(
    "/tmp/.sitesift-disabled-effect-adapter-report.json.tmp"
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

    def setUp(self):
        self._unlink_test_artifact(CANONICAL_OUTPUT_PATH)
        self._unlink_test_artifact(OWNED_TEMP_PATH)

    def tearDown(self):
        self._unlink_test_artifact(CANONICAL_OUTPUT_PATH)
        self._unlink_test_artifact(OWNED_TEMP_PATH)

    def _unlink_test_artifact(self, path):
        try:
            os.lstat(path)
        except FileNotFoundError:
            return
        os.unlink(path)

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

    def _seed_atomic_temporary(self):
        OWNED_TEMP_PATH.write_text('{"passed":true}\n', encoding="utf-8")
        return OWNED_TEMP_PATH

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
        self.assertEqual(
            CANONICAL_OUTPUT_PATH,
            getattr(self.script, "CANONICAL_OUTPUT_PATH", None),
        )
        self.assertEqual(
            OWNED_TEMP_PATH,
            getattr(self.script, "OWNED_TEMP_PATH", None),
        )

    def test_parser_rejects_output_abbreviation_and_cleans_explicit_output(self):
        self._seed_passed_report(CANONICAL_OUTPUT_PATH)

        completed = self._run_cli_subprocess(
            "--fixture",
            FIXTURE_PATH,
            "--runs",
            "3",
            "--out",
            "ignored-output-value",
            "--output",
            CANONICAL_OUTPUT_PATH,
            "--unexpected-option",
        )

        self.assertEqual(2, completed.returncode)
        self.assertRegex(
            completed.stderr,
            r"unrecognized arguments:.*--out",
        )
        self.assertFalse(CANONICAL_OUTPUT_PATH.exists())

    def test_arbitrary_output_path_is_rejected_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            arbitrary_path = Path(directory) / "passed-report.json"
            seeded = b'{"passed":true,"owner":"external"}\n'
            arbitrary_path.write_bytes(seeded)

            completed = self._run_cli_subprocess(
                "--fixture",
                FIXTURE_PATH,
                "--runs",
                "3",
                "--output",
                arbitrary_path,
            )

            self.assertEqual(2, completed.returncode)
            self.assertIn("canonical report output path", completed.stderr)
            self.assertEqual(seeded, arbitrary_path.read_bytes())

    def test_canonical_target_symlink_is_unlinked_without_following_target(self):
        with tempfile.TemporaryDirectory() as directory:
            symlink_target = Path(directory) / "external-proof.json"
            seeded = b'{"passed":true,"owner":"external"}\n'
            symlink_target.write_bytes(seeded)
            CANONICAL_OUTPUT_PATH.symlink_to(symlink_target)

            cleaned_path = self.script._remove_stale_output_artifacts(
                CANONICAL_OUTPUT_PATH
            )

            self.assertEqual(CANONICAL_OUTPUT_PATH, cleaned_path)
            self.assertFalse(CANONICAL_OUTPUT_PATH.exists())
            self.assertEqual(seeded, symlink_target.read_bytes())

    def test_unrelated_similarly_prefixed_sibling_is_never_removed(self):
        unrelated_path = Path(
            f"{OWNED_TEMP_PATH}.unrelated-{os.getpid()}"
        )
        self.addCleanup(self._unlink_test_artifact, unrelated_path)
        unrelated_path.write_bytes(b"not-owned\n")
        self._seed_passed_report(CANONICAL_OUTPUT_PATH)

        self.script._remove_stale_output_artifacts(CANONICAL_OUTPUT_PATH)

        self.assertFalse(CANONICAL_OUTPUT_PATH.exists())
        self.assertEqual(b"not-owned\n", unrelated_path.read_bytes())

    def test_owned_temp_symlink_collision_is_unlinked_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            symlink_target = Path(directory) / "external-temp-target"
            seeded = b"external-temp-content\n"
            symlink_target.write_bytes(seeded)
            self._seed_passed_report(CANONICAL_OUTPUT_PATH)
            OWNED_TEMP_PATH.symlink_to(symlink_target)

            with self.assertRaisesRegex(
                self.script.DryRunReportError,
                "temporary output collision",
            ):
                self.script._remove_stale_output_artifacts(
                    CANONICAL_OUTPUT_PATH
                )

            self.assertFalse(CANONICAL_OUTPUT_PATH.exists())
            self.assertFalse(OWNED_TEMP_PATH.exists())
            self.assertEqual(seeded, symlink_target.read_bytes())

    def test_atomic_write_orders_file_and_parent_directory_fsync(self):
        self.assertTrue(
            hasattr(self.script, "_fsync_parent_directory"),
            "runner must expose the parent-directory durability step",
        )
        events = []
        original_open = self.script.os.open
        original_fsync = self.script.os.fsync
        original_replace = self.script.os.replace
        original_close = self.script.os.close

        def open_spy(path, flags, mode=0o777):
            descriptor = original_open(path, flags, mode)
            kind = (
                "directory"
                if stat.S_ISDIR(os.fstat(descriptor).st_mode)
                else "file"
            )
            events.append(f"{kind}-open")
            return descriptor

        def fsync_spy(descriptor):
            kind = (
                "directory"
                if stat.S_ISDIR(os.fstat(descriptor).st_mode)
                else "file"
            )
            events.append(f"{kind}-fsync")
            return original_fsync(descriptor)

        def replace_spy(source, target):
            events.append("replace")
            return original_replace(source, target)

        def close_spy(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                events.append("directory-close")
            return original_close(descriptor)

        with mock.patch.object(
            self.script.os,
            "open",
            side_effect=open_spy,
        ), mock.patch.object(
            self.script.os,
            "fsync",
            side_effect=fsync_spy,
        ), mock.patch.object(
            self.script.os,
            "replace",
            side_effect=replace_spy,
        ), mock.patch.object(
            self.script.os,
            "close",
            side_effect=close_spy,
        ):
            self.script._atomic_write(CANONICAL_OUTPUT_PATH, b"proof\n")

        self.assertEqual(b"proof\n", CANONICAL_OUTPUT_PATH.read_bytes())
        self.assertLess(events.index("file-fsync"), events.index("replace"))
        self.assertLess(events.index("replace"), events.index("directory-open"))
        self.assertLess(
            events.index("directory-open"),
            events.index("directory-fsync"),
        )
        self.assertLess(
            events.index("directory-fsync"),
            events.index("directory-close"),
        )

    def test_parent_directory_fsync_failure_removes_passing_target(self):
        self.assertTrue(
            hasattr(self.script, "_fsync_parent_directory"),
            "runner must expose the parent-directory durability step",
        )

        with mock.patch.object(
            self.script,
            "_fsync_parent_directory",
            side_effect=OSError("injected directory fsync failure"),
        ), self.assertRaisesRegex(
            self.script.DryRunReportError,
            "parent directory fsync",
        ):
            self.script._atomic_write(CANONICAL_OUTPUT_PATH, b"proof\n")

        self.assertFalse(CANONICAL_OUTPUT_PATH.exists())
        self.assertFalse(OWNED_TEMP_PATH.exists())

    def test_transient_unlink_failure_is_retried_and_artifact_removed(self):
        self._seed_passed_report(CANONICAL_OUTPUT_PATH)
        original_unlink = self.script.os.unlink
        attempts = 0

        def transient_unlink(path):
            nonlocal attempts
            if Path(path) == CANONICAL_OUTPUT_PATH:
                attempts += 1
                if attempts == 1:
                    raise OSError("injected transient unlink failure")
            return original_unlink(path)

        with mock.patch.object(
            self.script.os,
            "unlink",
            side_effect=transient_unlink,
        ):
            self.script._remove_stale_output_artifacts(
                CANONICAL_OUTPUT_PATH
            )

        self.assertEqual(2, attempts)
        self.assertFalse(CANONICAL_OUTPUT_PATH.exists())

    def test_persistent_unlink_failure_is_controlled_and_names_artifact(self):
        self._seed_passed_report(CANONICAL_OUTPUT_PATH)

        with mock.patch.object(
            self.script.os,
            "unlink",
            side_effect=OSError("injected persistent unlink failure"),
        ), self.assertRaisesRegex(
            self.script.DryRunReportError,
            rf"cleanup.*{re.escape(str(CANONICAL_OUTPUT_PATH))}",
        ):
            self.script._remove_stale_output_artifacts(
                CANONICAL_OUTPUT_PATH
            )

        self.assertTrue(CANONICAL_OUTPUT_PATH.exists())

    def test_primary_write_and_cleanup_failures_share_one_controlled_error(self):
        with mock.patch.object(
            self.script.os,
            "replace",
            side_effect=OSError("injected replace failure"),
        ), mock.patch.object(
            self.script.os,
            "unlink",
            side_effect=OSError("injected cleanup failure"),
        ), self.assertRaisesRegex(
            self.script.DryRunReportError,
            rf"write.*cleanup.*{re.escape(str(OWNED_TEMP_PATH))}",
        ):
            self.script._atomic_write(CANONICAL_OUTPUT_PATH, b"proof\n")

        self.assertFalse(CANONICAL_OUTPUT_PATH.exists())
        self.assertTrue(OWNED_TEMP_PATH.exists())

    def test_cli_cleanup_failure_is_controlled_without_traceback(self):
        self._seed_passed_report(CANONICAL_OUTPUT_PATH)
        stderr = io.StringIO()

        with mock.patch.object(
            self.script.os,
            "unlink",
            side_effect=OSError("injected persistent unlink failure"),
        ), contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            self.script.main(
                (
                    "--fixture",
                    str(FIXTURE_PATH),
                    "--runs",
                    "3",
                    "--output",
                    str(CANONICAL_OUTPUT_PATH),
                    "--unexpected-option",
                )
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn(str(CANONICAL_OUTPUT_PATH), stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_source_tree_hash_depends_only_on_committed_tree_listing(self):
        parameters = tuple(
            inspect.signature(self.script._source_tree_hash).parameters
        )
        self.assertEqual((), parameters)
        tree_listing = (
            b"100644 blob aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            b"\tscripts/example.py\0"
        )
        expected = hashlib.sha256(
            len(tree_listing).to_bytes(8, "big") + tree_listing
        ).hexdigest()

        with mock.patch.object(
            self.script,
            "_git",
            return_value=tree_listing,
        ) as git:
            first = self.script._source_tree_hash()
            second = self.script._source_tree_hash()

        self.assertEqual(expected, first)
        self.assertEqual(first, second)
        self.assertEqual(2, git.call_count)
        for call in git.call_args_list:
            self.assertEqual("ls-tree", call.args[0])
            self.assertNotIn("revision-a", call.args)
            self.assertNotIn("revision-b", call.args)

    def test_report_has_exact_identity_counts_results_and_digest(self):
        report = self._run(CANONICAL_OUTPUT_PATH)

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
        report = self._run(CANONICAL_OUTPUT_PATH)

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
        report = self._run(CANONICAL_OUTPUT_PATH)

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
        first_report = self._run(CANONICAL_OUTPUT_PATH)
        first_bytes = CANONICAL_OUTPUT_PATH.read_bytes()
        second_report = self._run(CANONICAL_OUTPUT_PATH)

        self.assertEqual(first_report, second_report)
        self.assertEqual(first_bytes, CANONICAL_OUTPUT_PATH.read_bytes())

    def test_external_one_case_fixture_is_rejected_and_stale_output_removed(self):
        payload = self._fixture_payload()
        payload["cases"] = payload["cases"][:1]
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = self._write_fixture(directory, payload)
            output_path = CANONICAL_OUTPUT_PATH
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
            output_path = CANONICAL_OUTPUT_PATH

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
            output_path = CANONICAL_OUTPUT_PATH

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
                output_path = CANONICAL_OUTPUT_PATH
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
        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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

        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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

        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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
        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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
            output_path = CANONICAL_OUTPUT_PATH
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

        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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
        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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
        fault_patches = (
            mock.patch.object(
                self.script.os,
                "write",
                side_effect=OSError("injected temporary write failure"),
            ),
            mock.patch.object(
                self.script.os,
                "replace",
                side_effect=OSError("injected replace failure"),
            ),
        )
        for fault_patch in fault_patches:
            with self.subTest(fault=fault_patch):
                output_path = CANONICAL_OUTPUT_PATH
                self._seed_passed_report(output_path)
                with fault_patch, self.assertRaisesRegex(
                    self.script.DryRunReportError,
                    "write",
                ):
                    self._run(output_path)

                self.assertFalse(output_path.exists())
                self.assertFalse(OWNED_TEMP_PATH.exists())

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
        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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
            output_path = CANONICAL_OUTPUT_PATH
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
        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
            self._seed_atomic_temporary()
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
            self.assertIn("temporary output collision", completed.stderr)
            self.assertFalse(output_path.exists())
            self.assertFalse(OWNED_TEMP_PATH.exists())

    def test_subprocess_missing_required_arg_clears_supplied_output(self):
        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
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
        with tempfile.TemporaryDirectory():
            output_path = CANONICAL_OUTPUT_PATH
            self._seed_atomic_temporary()
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
            self.assertIn("temporary output collision", completed.stderr)
            self.assertFalse(CANONICAL_OUTPUT_PATH.exists())
            self.assertFalse(OWNED_TEMP_PATH.exists())

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
                first_path = CANONICAL_OUTPUT_PATH
                second_path = Path(directory) / "external.json"
                self._seed_passed_report(first_path)
                external_seed = b'{"passed":true,"owner":"external"}\n'
                if label == "conflicting":
                    second_path.write_bytes(external_seed)

                completed = self._run_cli_subprocess(
                    "--fixture",
                    FIXTURE_PATH,
                    "--runs",
                    "3",
                    *output_arguments(first_path, second_path),
                )

                self.assertEqual(2, completed.returncode)
                self.assertIn("--output", completed.stderr)
                self.assertFalse(first_path.exists())
                if label == "conflicting":
                    self.assertEqual(external_seed, second_path.read_bytes())

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
