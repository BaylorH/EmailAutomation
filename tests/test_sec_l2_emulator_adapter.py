import io
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_test_level, sec_l2_runner
from scripts.sec_l2_runner import SecL2Result, run_sec_l2


ADMIN_UI_COMMIT = "a" * 40
PINNED_FIRESTORE_SCRIPT = (
    "firebase emulators:exec --only firestore --project demo-sitesift-sec-l2 "
    '"node --test --test-reporter=tap '
    'tests/firestore-rules/firestore.rules.test.js"'
)
PASSING_TAP = """TAP version 13
ok 1 - owner access is enforced
ok 2 - cross-user access is denied
1..2
# tests 2
# suites 0
# pass 2
# fail 0
# cancelled 0
# skipped 0
# todo 0
"""
ZERO_TEST_TAP = """TAP version 13
1..0
# tests 0
# suites 0
# pass 0
# fail 0
# cancelled 0
# skipped 0
# todo 0
"""


class TestSecL2Runner(unittest.TestCase):
    def make_admin_ui_root(self, root: Path) -> None:
        (root / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "test:firestore-rules": PINNED_FIRESTORE_SCRIPT,
                    }
                }
            ),
            encoding="utf-8",
        )
        for relative_path in (
            "firebase.json",
            "firestore.indexes.json",
            "firestore.rules",
        ):
            (root / relative_path).write_text("{}", encoding="utf-8")
        rules_test = (
            root / "tests" / "firestore-rules" / "firestore.rules.test.js"
        )
        rules_test.parent.mkdir(parents=True)
        rules_test.write_text(
            "const committedRulesTestSource = true;\n",
            encoding="utf-8",
        )
        firebase_binary = root / "node_modules" / ".bin" / "firebase"
        firebase_binary.parent.mkdir(parents=True)
        firebase_binary.write_text("#!/bin/sh\n", encoding="utf-8")
        firebase_binary.chmod(0o755)

    @staticmethod
    def which(name: str) -> str:
        return f"/usr/bin/{name}"

    def successful_process(
        self,
        command: list[str],
        root: Path,
        *,
        npm_stdout: str = PASSING_TAP,
    ) -> subprocess.CompletedProcess[str]:
        if Path(command[0]) == sec_l2_runner.PORT_INSPECTOR_PATH:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="",
            )
        if command[1:] == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{root.resolve()}\n",
                stderr="",
            )
        if command[1:] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"{ADMIN_UI_COMMIT}\n",
                stderr="",
            )
        if len(command) > 1 and command[1] == "archive":
            output_argument = next(
                argument
                for argument in command
                if argument.startswith("--output=")
            )
            archive_path = Path(output_argument.split("=", 1)[1])
            separator_index = command.index("--")
            archived_paths = command[separator_index + 1 :]
            with tarfile.open(archive_path, "w") as archive:
                for relative_path in archived_paths:
                    archive.add(
                        root / relative_path,
                        arcname=relative_path,
                        recursive=False,
                    )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="",
            )
        if command[1:] == ["run", "test:firestore-rules", "--silent"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=npm_stdout,
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    @staticmethod
    def port_is_free(port: int) -> bool:
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
        return True

    def spawn_detached_emulator_probe(
        self,
        root: Path,
        *,
        port: int = 8080,
    ) -> subprocess.Popen[str]:
        ready_path = root / f"detached-emulator-{port}.ready"
        child_script = (
            "import pathlib, signal, socket, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "listener = socket.socket()\n"
            "listener.bind(('127.0.0.1', int(sys.argv[1])))\n"
            "listener.listen()\n"
            "pathlib.Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        process = subprocess.Popen(
            [
                str(Path(sys.executable).resolve()),
                "-c",
                child_script,
                str(port),
                str(ready_path),
                "-jar",
                "/tmp/cloud-firestore-emulator-v-test.jar",
                "--port",
                "8080",
                "--websocket_port",
                "9150",
                "--project_id",
                "demo-sitesift-sec-l2",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 3
        while not ready_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready_path.is_file():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
            self.fail("detached emulator probe did not become ready")
        return process

    @staticmethod
    def terminate_probe(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def test_missing_repository_is_unavailable(self):
        result = run_sec_l2(Path("/path/that/does/not/exist"))

        self.assertEqual(result.status, "unavailable")
        self.assertIn("repository", result.detail)

    def test_repository_missing_required_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / "package.json").write_text("{}", encoding="utf-8")
            (root / "firebase.json").write_text("{}", encoding="utf-8")

            result = run_sec_l2(root)

        self.assertEqual(result.status, "unavailable")
        self.assertIn("required files", result.detail)

    def test_missing_local_firebase_dependency_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            firebase_binary = root / "node_modules" / ".bin" / "firebase"
            firebase_binary.unlink()

            result = run_sec_l2(root)

        self.assertEqual(result.status, "unavailable")
        self.assertIn("dependencies", result.detail)

    def test_package_script_must_be_structurally_valid_and_demo_pinned(self):
        invalid_packages = (
            "{raw invalid package json",
            json.dumps([]),
            json.dumps({"scripts": {}}),
            json.dumps(
                {
                    "scripts": {
                        "test:firestore-rules": (
                            "firebase emulators:exec --project production-project"
                        )
                    }
                }
            ),
            json.dumps(
                {
                    "scripts": {
                        "test:firestore-rules": (
                            "firebase emulators:exec --project demo-safe "
                            "--project production-project"
                        )
                    }
                }
            ),
            json.dumps(
                {
                    "scripts": {
                        "test:firestore-rules": (
                            "firebase emulators:exec --only database "
                            "--project demo-safe "
                            '"node --test tests/firestore-rules/'
                            'firestore.rules.test.js"'
                        )
                    }
                }
            ),
            json.dumps(
                {
                    "scripts": {
                        "test:firestore-rules": (
                            "firebase emulators:exec --only firestore "
                            "--project demo-safe "
                            '"node --test tests/firestore-rules/'
                            'firestore.rules.test.js; curl example.com"'
                        )
                    }
                }
            ),
        )

        for package_text in invalid_packages:
            with self.subTest(package_text=package_text):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    root = Path(tmp_dir)
                    self.make_admin_ui_root(root)
                    (root / "package.json").write_text(
                        package_text,
                        encoding="utf-8",
                    )

                    result = run_sec_l2(
                        root,
                        runner=lambda command, **_kwargs: (
                            self.fail("invalid package reached npm execution")
                            if command[1:]
                            == ["run", "test:firestore-rules", "--silent"]
                            else self.successful_process(command, root)
                        ),
                        which=self.which,
                        java_fallback_dirs=(),
                    )

                self.assertEqual(result.status, "unavailable")
                self.assertIn("package", result.detail)
                self.assertNotIn(package_text, result.detail)

    def test_non_executable_local_firebase_cli_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            firebase_binary = root / "node_modules" / ".bin" / "firebase"
            firebase_binary.chmod(0o644)

            result = run_sec_l2(
                root,
                runner=lambda *_args, **_kwargs: self.fail(
                    "non-executable Firebase must fail before child execution"
                ),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("Firebase CLI", result.detail)

    def test_non_running_local_firebase_cli_is_unavailable_without_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            firebase_path = (root / "node_modules" / ".bin" / "firebase").resolve()

            def runner(command, **_kwargs):
                if command == [str(firebase_path), "--version"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout="raw Firebase credential output",
                        stderr="raw Firebase failure",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("Firebase CLI", result.detail)
        self.assertNotIn("raw Firebase", result.detail)

    def test_missing_command_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            result = run_sec_l2(root, which=lambda _name: None)

        self.assertEqual(result.status, "unavailable")
        self.assertIn("required command", result.detail)

    def test_non_running_command_is_unavailable_even_when_path_exists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                return subprocess.CompletedProcess(
                    command,
                    1 if command == ["/usr/bin/npm", "--version"] else 0,
                    stdout="raw runtime output",
                    stderr="raw runtime failure",
                )

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("required command", result.detail)
        self.assertIn("npm", result.detail)
        self.assertNotIn("raw runtime", result.detail)

    def test_runtime_probe_timeout_is_unavailable_without_exception_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                if command == ["/usr/bin/node", "--version"]:
                    raise subprocess.TimeoutExpired(
                        command,
                        1,
                        output="raw timed-out runtime output",
                        stderr="raw timed-out runtime error",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("node", result.detail)
        self.assertNotIn("raw timed-out", result.detail)

    def test_broken_primary_java_uses_running_homebrew_fallback(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            fallback_bin = root / "homebrew-openjdk" / "bin"
            fallback_bin.mkdir(parents=True)
            fallback_java = fallback_bin / "java"
            fallback_java.write_text("#!/bin/sh\n", encoding="utf-8")
            calls = []

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                if command == ["/usr/bin/java", "-version"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout="",
                        stderr="No Java runtime present",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(fallback_bin,),
            )

        self.assertEqual(result.status, "passed")
        self.assertIn(
            ([str(fallback_java.resolve()), "-version"]),
            [command for command, _kwargs in calls],
        )
        npm_call = next(
            call
            for call in calls
            if call[0][1:] == ["run", "test:firestore-rules", "--silent"]
        )
        self.assertEqual(
            npm_call[1]["env"]["PATH"].split(os.pathsep)[0],
            str(fallback_bin.resolve()),
        )

    def test_failed_emulator_suite_is_failed_without_raw_child_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                if command[1:] == ["run", "test:firestore-rules", "--silent"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout="raw synthetic document payload",
                        stderr="raw synthetic failure",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertNotIn("payload", result.detail)
        self.assertNotIn("raw synthetic failure", result.detail)

    def test_nonzero_suite_exit_records_error_even_with_passing_tap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                process = self.successful_process(command, root)
                if command[1:] == ["run", "test:firestore-rules", "--silent"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        stdout=PASSING_TAP,
                        stderr="raw emulator shutdown failure",
                    )
                return process

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertNotIn("raw emulator", result.detail)

    def test_repository_toplevel_must_equal_supplied_root_without_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                if command[1:] == ["rev-parse", "--show-toplevel"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="/raw/wrong/repository\n",
                        stderr="raw git root failure",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("repository root", result.detail)
        self.assertNotIn("/raw/wrong", result.detail)
        self.assertNotIn("raw git", result.detail)
        self.assertNotIn(
            ["run", "test:firestore-rules", "--silent"],
            [command[1:] for command in calls],
        )

    def test_dirty_repository_is_unavailable_without_raw_porcelain_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            calls = []

            def runner(command, **_kwargs):
                calls.append(command)
                if command[1:] == ["status", "--porcelain"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=" M firestore.rules\n?? raw-secret.json\n",
                        stderr="raw git status failure",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("clean", result.detail)
        self.assertNotIn("firestore.rules", result.detail)
        self.assertNotIn("raw-secret", result.detail)
        self.assertNotIn(
            ["rev-parse", "HEAD"],
            [command[1:] for command in calls],
        )

    def test_git_timeout_is_unavailable_without_exception_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                if command[1:] == ["rev-parse", "--show-toplevel"]:
                    raise subprocess.TimeoutExpired(
                        command,
                        1,
                        output="raw git timeout output",
                        stderr="raw git timeout error",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("repository", result.detail)
        self.assertNotIn("raw git timeout", result.detail)

    def test_invalid_commit_identity_is_unavailable_without_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                if command[1:] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="raw invalid commit identity",
                        stderr="raw git failure",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("commit identity", result.detail)
        self.assertNotIn("raw invalid", result.detail)
        self.assertNotIn("raw git", result.detail)

    def test_non_text_commit_identity_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                if command[1:] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=None,
                        stderr=None,
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("commit identity", result.detail)

    def test_missing_tap_summary_is_failed_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                return self.successful_process(
                    command,
                    root,
                    npm_stdout="emulator exited",
                )

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertIn("TAP summary", result.detail)

    def test_non_text_tap_output_is_failed_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                if command[1:] == ["run", "test:firestore-rules", "--silent"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=None,
                        stderr=None,
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertIn("TAP summary", result.detail)

    def test_tap_requires_one_complete_summary_and_matching_plan(self):
        invalid_tap_outputs = (
            PASSING_TAP + PASSING_TAP,
            PASSING_TAP.replace("1..2", "1..3"),
            PASSING_TAP.replace("# cancelled 0\n", ""),
            PASSING_TAP.replace("# todo 0\n", ""),
            PASSING_TAP.replace("# fail 0", "# fail 1"),
            PASSING_TAP.replace("# cancelled 0", "# cancelled 1"),
            PASSING_TAP.replace("# skipped 0", "# skipped 1"),
            PASSING_TAP.replace("# todo 0", "# todo 1"),
        )

        for tap_output in invalid_tap_outputs:
            with self.subTest(tap_output=tap_output):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    root = Path(tmp_dir)
                    self.make_admin_ui_root(root)

                    def runner(command, **_kwargs):
                        return self.successful_process(
                            command,
                            root,
                            npm_stdout=tap_output,
                        )

                    result = run_sec_l2(
                        root,
                        runner=runner,
                        which=self.which,
                        java_fallback_dirs=(),
                    )

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.errors, 1)
                self.assertIn("TAP", result.detail)

    def test_zero_test_tap_summary_is_failed_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)

            def runner(command, **_kwargs):
                return self.successful_process(
                    command,
                    root,
                    npm_stdout=ZERO_TEST_TAP,
                )

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.tests_run, 0)
        self.assertIn("not completely passing", result.detail)

    def test_tap_assertions_must_reconcile_with_footer_counts(self):
        contradictory_footer = PASSING_TAP.replace(
            "# pass 2\n# fail 0",
            "# pass 1\n# fail 1",
        )

        self.assertIsNone(
            sec_l2_runner._parse_tap_counts(contradictory_footer)
        )

    def test_tap_assertions_must_match_plan_without_extra_records(self):
        two_assertions_one_plan = PASSING_TAP.replace(
            "1..2\n# tests 2\n",
            "1..1\n# tests 1\n",
        ).replace("# pass 2", "# pass 1")

        self.assertIsNone(
            sec_l2_runner._parse_tap_counts(two_assertions_one_plan)
        )

    def test_tap_assertions_reject_not_ok_duplicate_and_missing_records(self):
        invalid_assertions = (
            PASSING_TAP.replace("ok 2", "not ok 2").replace(
                "# pass 2\n# fail 0",
                "# pass 1\n# fail 1",
            ),
            PASSING_TAP.replace("ok 2", "ok 1"),
            PASSING_TAP.replace(
                "ok 2 - cross-user access is denied\n",
                "",
            ),
        )

        for tap_output in invalid_assertions:
            with self.subTest(tap_output=tap_output):
                self.assertIsNone(
                    sec_l2_runner._parse_tap_counts(tap_output)
                )

    def test_tap_rejects_top_level_bail_out_lines_case_insensitively(self):
        bail_out_lines = (
            "Bail out!",
            "Bail out! emulator startup failed",
            "bAiL OuT! mixed-case reason",
        )

        for bail_out_line in bail_out_lines:
            with self.subTest(bail_out_line=bail_out_line):
                tap_output = PASSING_TAP.replace(
                    "1..2\n",
                    f"{bail_out_line}\n1..2\n",
                )
                self.assertIsNone(
                    sec_l2_runner._parse_tap_counts(tap_output)
                )

    def test_tap_rejects_skip_assertion_directives_but_not_description_words(self):
        skip_directives = (
            "# SKIP",
            "# skip unsupported runtime",
            "# SkIp mixed-case reason",
        )

        for directive in skip_directives:
            with self.subTest(directive=directive):
                tap_output = PASSING_TAP.replace(
                    "ok 1 - owner access is enforced",
                    "ok 1 - owner access is enforced " + directive,
                )
                self.assertIsNone(
                    sec_l2_runner._parse_tap_counts(tap_output)
                )

        ordinary_description = PASSING_TAP.replace(
            "ok 1 - owner access is enforced",
            "ok 1 - skip cache refresh remains enforced",
        )
        self.assertIsNotNone(
            sec_l2_runner._parse_tap_counts(ordinary_description)
        )

    def test_tap_rejects_todo_assertion_directives_but_not_description_words(self):
        todo_directives = (
            "# TODO",
            "# todo implement later",
            "# ToDo mixed-case reason",
        )

        for directive in todo_directives:
            with self.subTest(directive=directive):
                tap_output = PASSING_TAP.replace(
                    "ok 2 - cross-user access is denied",
                    "ok 2 - cross-user access is denied " + directive,
                )
                self.assertIsNone(
                    sec_l2_runner._parse_tap_counts(tap_output)
                )

        ordinary_description = PASSING_TAP.replace(
            "ok 2 - cross-user access is denied",
            "ok 2 - todo records remain cross-user denied",
        )
        self.assertIsNotNone(
            sec_l2_runner._parse_tap_counts(ordinary_description)
        )

    def test_emulator_timeout_is_failed_with_duration_and_no_raw_output(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            clock_values = iter((10.0, 12.5))

            def runner(command, **_kwargs):
                if command[1:] == ["run", "test:firestore-rules", "--silent"]:
                    raise subprocess.TimeoutExpired(
                        command,
                        1,
                        output="raw emulator timeout output",
                        stderr="raw emulator timeout error",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
                clock=lambda: next(clock_values),
            )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.duration_ms, 2500)
        self.assertIn("timed out", result.detail)
        self.assertNotIn("raw emulator", result.detail)

    def test_emulator_file_mutation_is_rejected_after_passing_tap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            firestore_rules = root / "firestore.rules"
            status_calls = 0
            clock_values = iter((30.0, 30.25))

            def runner(command, **_kwargs):
                nonlocal status_calls
                if command[1:] == ["run", "test:firestore-rules", "--silent"]:
                    firestore_rules.write_text(
                        "raw mutated Firestore rules",
                        encoding="utf-8",
                    )
                    return self.successful_process(command, root)
                if command[1:] == ["status", "--porcelain"]:
                    status_calls += 1
                    if "mutated" in firestore_rules.read_text(encoding="utf-8"):
                        return subprocess.CompletedProcess(
                            command,
                            0,
                            stdout=" M firestore.rules\n",
                            stderr="raw post-run git output",
                        )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
                clock=lambda: next(clock_values),
            )

        self.assertEqual(status_calls, 2)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.duration_ms, 250)
        self.assertIn("changed during", result.detail)
        self.assertNotIn("firestore.rules", result.detail)
        self.assertNotIn("raw post-run", result.detail)

    def test_mutate_restore_worktree_cannot_change_committed_snapshot_source(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            rules_path = root / "firestore.rules"
            package_path = root / "package.json"
            committed_rules = "committed Firestore rules"
            rules_path.write_text(committed_rules, encoding="utf-8")
            committed_package = package_path.read_text(encoding="utf-8")
            malicious_package = json.dumps(
                {
                    "scripts": {
                        "test:firestore-rules": (
                            "node -e \"process.exitCode = 0\""
                        )
                    }
                }
            )
            observed = {}

            def runner(command, **kwargs):
                if command[1:] == ["run", "test:firestore-rules", "--silent"]:
                    rules_path.write_text(
                        "mutated rules that must not be tested",
                        encoding="utf-8",
                    )
                    package_path.write_text(
                        malicious_package,
                        encoding="utf-8",
                    )
                    execution_root = Path(kwargs["cwd"])
                    observed["execution_root"] = execution_root
                    observed["rules"] = (
                        execution_root / "firestore.rules"
                    ).read_text(encoding="utf-8")
                    observed["package"] = json.loads(
                        (execution_root / "package.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    rules_path.write_text(committed_rules, encoding="utf-8")
                    package_path.write_text(
                        committed_package,
                        encoding="utf-8",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(result.status, "passed")
        self.assertNotEqual(observed["execution_root"], root.resolve())
        self.assertEqual(observed["rules"], committed_rules)
        self.assertEqual(
            observed["package"]["scripts"]["test:firestore-rules"],
            PINNED_FIRESTORE_SCRIPT,
        )
        self.assertEqual(result.admin_ui_commit, ADMIN_UI_COMMIT)

    def test_emulator_head_change_is_rejected_after_passing_tap(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            head_calls = 0

            def runner(command, **_kwargs):
                nonlocal head_calls
                if command[1:] == ["rev-parse", "HEAD"]:
                    head_calls += 1
                    commit = ADMIN_UI_COMMIT if head_calls == 1 else "b" * 40
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout=f"{commit}\n",
                        stderr="raw changed HEAD output",
                    )
                return self.successful_process(command, root)

            result = run_sec_l2(
                root,
                runner=runner,
                which=self.which,
                java_fallback_dirs=(),
            )

        self.assertEqual(head_calls, 2)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertIn("changed during", result.detail)
        self.assertNotIn("raw changed", result.detail)

    def test_preexisting_emulator_signature_is_unavailable_and_never_killed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            self.assertTrue(self.port_is_free(8080))
            self.assertTrue(self.port_is_free(9150))
            probe = self.spawn_detached_emulator_probe(root)
            probe_survived = False
            try:
                def runner(command, **kwargs):
                    if Path(command[0]) == Path("/bin/ps"):
                        return subprocess.run(command, **kwargs)
                    return self.successful_process(command, root)

                result = run_sec_l2(
                    root,
                    runner=runner,
                    which=self.which,
                    java_fallback_dirs=(),
                )
                probe_survived = probe.poll() is None
            finally:
                self.terminate_probe(probe)

        self.assertEqual(result.status, "unavailable")
        self.assertIn("preflight", result.detail)
        self.assertTrue(probe_survived)

    def test_detached_emulator_is_cleaned_and_cannot_return_passed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            self.assertTrue(self.port_is_free(8080))
            self.assertTrue(self.port_is_free(9150))
            spawned_probe = None

            def runner(command, **kwargs):
                nonlocal spawned_probe
                if Path(command[0]) == Path("/bin/ps"):
                    return subprocess.run(command, **kwargs)
                if command[1:] == ["run", "test:firestore-rules", "--silent"]:
                    spawned_probe = self.spawn_detached_emulator_probe(root)
                return self.successful_process(command, root)

            try:
                with patch.object(
                    sec_l2_runner,
                    "TERMINATE_GRACE_SECONDS",
                    0.2,
                ):
                    result = run_sec_l2(
                        root,
                        runner=runner,
                        which=self.which,
                        java_fallback_dirs=(),
                    )

                self.assertIsNotNone(spawned_probe)
                exit_deadline = time.monotonic() + 3
                while (
                    spawned_probe.poll() is None
                    and time.monotonic() < exit_deadline
                ):
                    time.sleep(0.05)
                child_is_gone = spawned_probe.poll() is not None
                port_is_released = self.port_is_free(8080)
            finally:
                if spawned_probe is not None:
                    self.terminate_probe(spawned_probe)

        self.assertNotEqual(result.status, "passed")
        self.assertEqual(result.status, "failed")
        self.assertIn("lifecycle", result.detail)
        self.assertTrue(child_is_gone)
        self.assertTrue(port_is_released)
        self.assertTrue(self.port_is_free(9150))

    def test_postflight_accepts_closed_port_during_tcp_teardown(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="",
                stderr="",
            )

        with patch.object(
            sec_l2_runner,
            "_emulator_ports_are_free",
            return_value=False,
        ), patch.object(
            sec_l2_runner,
            "_emulator_ports_are_closed",
            return_value=True,
            create=True,
        ):
            result = sec_l2_runner._verify_emulator_lifecycle(
                runner,
                environment={},
                baseline_pids=frozenset(),
                quiescence_seconds=0,
                max_settle_seconds=0.01,
                poll_interval_seconds=0.001,
                terminate_grace_seconds=0.01,
            )

        self.assertFalse(result.violation)
        self.assertFalse(result.cleanup_failed)

    def test_postflight_port_inspector_fails_closed_on_tool_error(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                2,
                stdout="raw unexpected inspector output",
                stderr="raw inspector failure",
            )

        ports_are_closed = sec_l2_runner._emulator_ports_are_closed(
            runner,
            environment={},
        )

        self.assertIsNone(ports_are_closed)

    def test_preflight_bind_probe_allows_safe_port_reuse(self):
        probe = unittest.mock.MagicMock()
        probe.__enter__.return_value = probe

        with patch.object(
            sec_l2_runner.socket,
            "socket",
            return_value=probe,
        ):
            ports_are_free = sec_l2_runner._emulator_ports_are_free()

        self.assertTrue(ports_are_free)
        self.assertEqual(probe.bind.call_count, len(sec_l2_runner.EMULATOR_PORTS))
        self.assertEqual(
            probe.setsockopt.call_args_list,
            [
                unittest.mock.call(
                    socket.SOL_SOCKET,
                    socket.SO_REUSEADDR,
                    1,
                )
                for _port in sec_l2_runner.EMULATOR_PORTS
            ],
        )

    def test_process_group_timeout_removes_spawned_grandchild(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            grandchild_pid_path = root / "grandchild.pid"
            parent_script = (
                "import pathlib, signal, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)'], stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)\n"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(child.pid), encoding='utf-8')\n"
                "print('raw process-group output', flush=True)\n"
                "time.sleep(60)\n"
            )

            result = sec_l2_runner._run_emulator_process_group(
                [
                    str(Path(sys.executable).resolve()),
                    "-c",
                    parent_script,
                    str(grandchild_pid_path),
                ],
                cwd=root,
                environment={
                    "CI": "true",
                    "HOME": str(root),
                    "NPM_CONFIG_CACHE": str(root / "npm-cache"),
                    "PATH": "/usr/bin:/bin",
                    "TMPDIR": str(root),
                },
                timeout_seconds=1,
                terminate_grace_seconds=1,
            )

            self.assertTrue(grandchild_pid_path.is_file())
            grandchild_pid = int(
                grandchild_pid_path.read_text(encoding="utf-8")
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(grandchild_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.fail("grandchild process survived process-group timeout")

        self.assertTrue(result.timed_out)
        self.assertFalse(result.cleanup_failed)
        self.assertNotIn("raw process-group", result.stdout)

    def test_process_group_cleanup_failure_is_failed_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            clock_values = iter((40.0, 41.0))

            def runner(command, **_kwargs):
                return self.successful_process(command, root)

            cleanup_failure = sec_l2_runner._EmulatorProcessResult(
                timed_out=True,
                cleanup_failed=True,
            )
            with patch.object(
                sec_l2_runner.subprocess,
                "run",
                side_effect=runner,
            ), patch.object(
                sec_l2_runner,
                "_run_emulator_process_group",
                return_value=cleanup_failure,
            ):
                result = run_sec_l2(
                    root,
                    which=self.which,
                    java_fallback_dirs=(),
                    clock=lambda: next(clock_values),
                )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.duration_ms, 1000)
        self.assertIn("cleanup failed", result.detail)

    def test_normal_parent_exit_cleans_residual_process_group(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            grandchild_pid_path = root / "normal-exit-grandchild.pid"
            grandchild_ready_path = root / "normal-exit-grandchild.ready"
            child_script = (
                "import pathlib, signal, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "pathlib.Path(sys.argv[1]).write_text('ready', encoding='utf-8')\n"
                "time.sleep(60)\n"
            )
            parent_script = (
                "import pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "sys.argv[3], sys.argv[2]], stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)\n"
                "ready = pathlib.Path(sys.argv[2])\n"
                "while not ready.is_file(): time.sleep(0.01)\n"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(child.pid), encoding='utf-8')\n"
            )

            cleanup_started = time.monotonic()
            result = sec_l2_runner._run_emulator_process_group(
                [
                    str(Path(sys.executable).resolve()),
                    "-c",
                    parent_script,
                    str(grandchild_pid_path),
                    str(grandchild_ready_path),
                    child_script,
                ],
                cwd=root,
                environment={
                    "CI": "true",
                    "HOME": str(root),
                    "NPM_CONFIG_CACHE": str(root / "npm-cache"),
                    "PATH": "/usr/bin:/bin",
                    "TMPDIR": str(root),
                },
                timeout_seconds=5,
                terminate_grace_seconds=1,
                normal_exit_quiescence_seconds=0.2,
                normal_exit_max_settle_seconds=0.5,
                process_group_poll_interval_seconds=0.02,
            )
            cleanup_elapsed = time.monotonic() - cleanup_started

            self.assertTrue(grandchild_pid_path.is_file())
            grandchild_pid = int(
                grandchild_pid_path.read_text(encoding="utf-8")
            )
            deadline = time.monotonic() + 5
            child_is_gone = False
            while time.monotonic() < deadline:
                try:
                    os.kill(grandchild_pid, 0)
                except ProcessLookupError:
                    child_is_gone = True
                    break
                time.sleep(0.05)
            if not child_is_gone:
                try:
                    os.kill(grandchild_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.assertTrue(child_is_gone)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.descendants_remained)
        self.assertFalse(result.cleanup_failed)
        self.assertGreaterEqual(cleanup_elapsed, 0.8)

    def test_brief_normal_exit_descendant_can_quiesce_naturally(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            child_ready_path = root / "brief-child.ready"
            parent_script = (
                "import pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "\"import pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text("
                "'ready', encoding='utf-8'); time.sleep(0.3)\", "
                "sys.argv[1]], stdout=subprocess.DEVNULL, "
                "stderr=subprocess.DEVNULL)\n"
                "ready = pathlib.Path(sys.argv[1])\n"
                "while not ready.is_file(): time.sleep(0.01)\n"
            )

            result = sec_l2_runner._run_emulator_process_group(
                [
                    str(Path(sys.executable).resolve()),
                    "-c",
                    parent_script,
                    str(child_ready_path),
                ],
                cwd=root,
                environment={
                    "CI": "true",
                    "HOME": str(root),
                    "NPM_CONFIG_CACHE": str(root / "npm-cache"),
                    "PATH": "/usr/bin:/bin",
                    "TMPDIR": str(root),
                },
                timeout_seconds=5,
                terminate_grace_seconds=0.2,
                normal_exit_quiescence_seconds=0.2,
                normal_exit_max_settle_seconds=0.8,
                process_group_poll_interval_seconds=0.02,
            )

        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.descendants_remained)
        self.assertFalse(result.cleanup_failed)

    def test_delayed_descendant_visibility_repeatedly_cleans_process_and_port(self):
        actual_group_exists = sec_l2_runner._process_group_exists
        lifecycle_results = []

        for iteration in range(2):
            with self.subTest(iteration=iteration):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    root = Path(tmp_dir)
                    process_details_path = root / "process-details"
                    child_ready_path = root / "child-ready"
                    with socket.socket() as port_reservation:
                        port_reservation.bind(("127.0.0.1", 0))
                        port = port_reservation.getsockname()[1]

                    child_script = (
                        "import pathlib, signal, socket, sys, time\n"
                        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                        "listener = socket.socket()\n"
                        "listener.bind(('127.0.0.1', int(sys.argv[2])))\n"
                        "listener.listen()\n"
                        "pathlib.Path(sys.argv[1]).write_text("
                        "'ready', encoding='utf-8')\n"
                        "time.sleep(60)\n"
                    )
                    parent_script = (
                        "import os, pathlib, subprocess, sys, time\n"
                        "child = subprocess.Popen([sys.executable, '-c', "
                        "sys.argv[4], sys.argv[2], sys.argv[3]], "
                        "stdout=subprocess.DEVNULL, "
                        "stderr=subprocess.DEVNULL)\n"
                        "ready = pathlib.Path(sys.argv[2])\n"
                        "while not ready.is_file(): time.sleep(0.01)\n"
                        "pathlib.Path(sys.argv[1]).write_text("
                        "f'{child.pid} {os.getpgrp()}', encoding='utf-8')\n"
                    )
                    hidden_checks = 1

                    def delayed_group_exists(process_group_id):
                        nonlocal hidden_checks
                        if hidden_checks:
                            hidden_checks -= 1
                            return False
                        return actual_group_exists(process_group_id)

                    child_pid = None
                    process_group_id = None
                    try:
                        with patch.object(
                            sec_l2_runner,
                            "_process_group_exists",
                            side_effect=delayed_group_exists,
                        ):
                            result = sec_l2_runner._run_emulator_process_group(
                                [
                                    str(Path(sys.executable).resolve()),
                                    "-c",
                                    parent_script,
                                    str(process_details_path),
                                    str(child_ready_path),
                                    str(port),
                                    child_script,
                                ],
                                cwd=root,
                                environment={
                                    "CI": "true",
                                    "HOME": str(root),
                                    "NPM_CONFIG_CACHE": str(root / "npm-cache"),
                                    "PATH": "/usr/bin:/bin",
                                    "TMPDIR": str(root),
                                },
                                timeout_seconds=5,
                                terminate_grace_seconds=0.2,
                                normal_exit_quiescence_seconds=0.2,
                                normal_exit_max_settle_seconds=0.5,
                                process_group_poll_interval_seconds=0.02,
                            )

                        child_pid, process_group_id = (
                            int(value)
                            for value in process_details_path.read_text(
                                encoding="utf-8"
                            ).split()
                        )
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            try:
                                os.kill(child_pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.05)
                        child_is_gone = not actual_group_exists(
                            process_group_id
                        )
                        with socket.socket() as port_probe:
                            try:
                                port_probe.bind(("127.0.0.1", port))
                            except OSError:
                                port_is_released = False
                            else:
                                port_is_released = True
                    finally:
                        if (
                            process_group_id is not None
                            and actual_group_exists(process_group_id)
                        ):
                            try:
                                os.killpg(process_group_id, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            cleanup_deadline = time.monotonic() + 3
                            while (
                                actual_group_exists(process_group_id)
                                and time.monotonic() < cleanup_deadline
                            ):
                                time.sleep(0.05)

                    lifecycle_results.append(
                        (
                            result,
                            child_is_gone,
                            port_is_released,
                            process_group_id,
                        )
                    )

        self.assertEqual(len(lifecycle_results), 2)
        for result, child_is_gone, port_is_released, process_group_id in (
            lifecycle_results
        ):
            self.assertEqual(result.returncode, 0)
            self.assertTrue(result.descendants_remained)
            self.assertFalse(result.cleanup_failed)
            self.assertTrue(child_is_gone)
            self.assertTrue(port_is_released)
            self.assertFalse(actual_group_exists(process_group_id))

    def test_residual_process_group_maps_passing_tap_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            clock_values = iter((50.0, 50.5))

            def runner(command, **_kwargs):
                return self.successful_process(command, root)

            residual_result = sec_l2_runner._EmulatorProcessResult(
                returncode=0,
                stdout=PASSING_TAP,
                descendants_remained=True,
            )
            with patch.object(
                sec_l2_runner.subprocess,
                "run",
                side_effect=runner,
            ), patch.object(
                sec_l2_runner,
                "_run_emulator_process_group",
                return_value=residual_result,
            ):
                result = run_sec_l2(
                    root,
                    which=self.which,
                    java_fallback_dirs=(),
                    clock=lambda: next(clock_values),
                )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.duration_ms, 500)
        self.assertIn("descendants remained", result.detail)
        self.assertNotEqual(result.status, "passed")

    def test_passing_suite_parses_tap_and_removes_sensitive_environment(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self.make_admin_ui_root(root)
            calls = []
            poisoned_environment = {
                name: "must-not-propagate"
                for name in set(run_test_level.SENSITIVE_ENV_NAMES)
                | sec_l2_runner.BLOCKED_FIREBASE_ENV_NAMES
            }
            poisoned_environment.update(
                {
                    "AWS_PROFILE": "production",
                    "CLOUDSDK_CONFIG": "/attacker/gcloud",
                    "FIREBASE_TOKEN": "must-not-propagate",
                    "GOOGLE_APPLICATION_CREDENTIALS": "/attacker/credentials.json",
                    "GOOGLE_CLOUD_QUOTA_PROJECT": "production-project",
                    "HOME": "/attacker/home",
                    "JAVA_TOOL_OPTIONS": "-javaagent:/attacker/agent.jar",
                    "NODE_OPTIONS": "--require /attacker/inject.js",
                    "NPM_CONFIG_USERCONFIG": "/attacker/.npmrc",
                    "PATH": "/attacker/bin",
                    "PYTHONPATH": "/attacker/python",
                }
            )
            allowed_child_environment = {
                "CI",
                "HOME",
                "NPM_CONFIG_CACHE",
                "PATH",
                "TMPDIR",
            }
            clock_values = iter((20.0, 20.125))
            snapshot_observation = {}
            self.assertEqual(
                sec_l2_runner.SENSITIVE_ENV_NAMES,
                run_test_level.SENSITIVE_ENV_NAMES,
            )

            def runner(command, **kwargs):
                calls.append((command, kwargs))
                child_environment = kwargs["env"]
                self.assertEqual(
                    set(child_environment),
                    allowed_child_environment,
                )
                self.assertNotIn(
                    "must-not-propagate",
                    child_environment.values(),
                )
                self.assertFalse(
                    any(
                        "/attacker" in value
                        for value in child_environment.values()
                    )
                )
                if command[1:] == [
                    "run",
                    "test:firestore-rules",
                    "--silent",
                ]:
                    snapshot_root = Path(kwargs["cwd"])
                    dependency_link = snapshot_root / "node_modules"
                    snapshot_observation.update(
                        {
                            "root": snapshot_root,
                            "dependency_is_link": dependency_link.is_symlink(),
                            "dependency_target": dependency_link.resolve(),
                            "package_is_read_only": (
                                (snapshot_root / "package.json").stat().st_mode
                                & 0o222
                                == 0
                            ),
                            "rules_are_read_only": (
                                (snapshot_root / "firestore.rules").stat().st_mode
                                & 0o222
                                == 0
                            ),
                        }
                    )
                return self.successful_process(command, root)

            with patch.dict(
                os.environ,
                poisoned_environment,
                clear=False,
            ):
                result = run_sec_l2(
                    root,
                    runner=runner,
                    which=self.which,
                    java_fallback_dirs=(),
                    clock=lambda: next(clock_values),
                )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.tests_run, 2)
        self.assertEqual(result.failures, 0)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.duration_ms, 125)
        self.assertEqual(result.admin_ui_commit, ADMIN_UI_COMMIT)
        npm_command, npm_kwargs = next(
            call
            for call in calls
            if call[0][1:] == ["run", "test:firestore-rules", "--silent"]
        )
        self.assertEqual(
            npm_command[1:],
            ["run", "test:firestore-rules", "--silent"],
        )
        self.assertNotEqual(npm_kwargs["cwd"], root.resolve())
        self.assertEqual(
            npm_kwargs["cwd"],
            snapshot_observation["root"],
        )
        self.assertTrue(snapshot_observation["dependency_is_link"])
        self.assertEqual(
            snapshot_observation["dependency_target"],
            (root / "node_modules").resolve(),
        )
        self.assertTrue(snapshot_observation["package_is_read_only"])
        self.assertTrue(snapshot_observation["rules_are_read_only"])
        self.assertFalse(Path(npm_kwargs["env"]["HOME"]).exists())
        self.assertNotIn("/attacker", npm_kwargs["env"]["PATH"])

        commands = [command for command, _kwargs in calls]
        firebase_path = str(
            (root / "node_modules" / ".bin" / "firebase").resolve()
        )
        for expected_probe in (
            ["/usr/bin/node", "--version"],
            ["/usr/bin/npm", "--version"],
            ["/usr/bin/git", "--version"],
            ["/usr/bin/java", "-version"],
            [firebase_path, "--version"],
        ):
            self.assertIn(expected_probe, commands)
        self.assertTrue(all(Path(command[0]).is_absolute() for command in commands))
        self.assertNotIn("git", [command[0] for command in commands])
        self.assertNotIn("npm", [command[0] for command in commands])
        self.assertNotIn("firebase", [command[0] for command in commands])

        command_args = [command[1:] for command in commands]
        root_index = command_args.index(["rev-parse", "--show-toplevel"])
        status_index = command_args.index(["status", "--porcelain"])
        head_index = command_args.index(["rev-parse", "HEAD"])
        archive_index = next(
            index
            for index, command in enumerate(command_args)
            if command and command[0] == "archive"
        )
        npm_index = command_args.index(
            ["run", "test:firestore-rules", "--silent"]
        )
        self.assertLess(root_index, status_index)
        self.assertLess(status_index, head_index)
        self.assertLess(head_index, archive_index)
        self.assertLess(archive_index, npm_index)
        self.assertLess(head_index, npm_index)

        for command, kwargs in calls:
            if (
                command[1:] in (
                    ["rev-parse", "--show-toplevel"],
                    ["status", "--porcelain"],
                    ["rev-parse", "HEAD"],
                )
                or command[1:2] == ["archive"]
            ):
                self.assertEqual(
                    kwargs["timeout"],
                    sec_l2_runner.GIT_TIMEOUT_SECONDS,
                )
            elif command[1:] == ["run", "test:firestore-rules", "--silent"]:
                self.assertEqual(
                    kwargs["timeout"],
                    sec_l2_runner.EMULATOR_TIMEOUT_SECONDS,
                )
            else:
                self.assertEqual(
                    kwargs["timeout"],
                    sec_l2_runner.PROBE_TIMEOUT_SECONDS,
                )

        self.assertGreaterEqual(sec_l2_runner.PROBE_TIMEOUT_SECONDS, 5)
        self.assertGreaterEqual(sec_l2_runner.GIT_TIMEOUT_SECONDS, 5)
        self.assertGreaterEqual(sec_l2_runner.EMULATOR_TIMEOUT_SECONDS, 300)

    def test_configured_l2_dispatches_and_prints_sanitized_summary(self):
        registry = {
            "levels": {
                "L2": {
                    "availability": "environment_required",
                    "requiredEnvironment": ["SITESIFT_ADMIN_UI_ROOT"],
                }
            }
        }
        passing = SecL2Result(
            status="passed",
            tests_run=2,
            failures=0,
            errors=0,
            skipped=0,
            duration_ms=125,
            admin_ui_commit=ADMIN_UI_COMMIT,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(
                os.environ,
                {"SITESIFT_ADMIN_UI_ROOT": tmp_dir},
                clear=False,
            ), patch.object(
                run_test_level,
                "run_sec_l2",
                return_value=passing,
            ):
                result = run_test_level.run_level(
                    "L2",
                    registry_path=registry_path,
                    output=output,
                )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.tests_run, 2)
        summary = output.getvalue().strip()
        self.assertTrue(
            summary.startswith(
                "L2 PASSED family=SEC scenario=SEC-01 "
                "tests=2 failures=0 errors=0 skipped=0"
            )
        )
        self.assertIn("duration_ms=125", summary)
        self.assertTrue(summary.endswith(f"admin_ui_commit={ADMIN_UI_COMMIT}"))

    def test_configured_l2_missing_root_is_unavailable_before_dispatch(self):
        registry = {
            "levels": {
                "L2": {
                    "availability": "environment_required",
                    "requiredEnvironment": ["SITESIFT_ADMIN_UI_ROOT"],
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch.object(
                run_test_level,
                "run_sec_l2",
            ) as run_sec_l2_mock:
                result = run_test_level.run_level(
                    "L2",
                    registry_path=registry_path,
                    output=output,
                )

        run_sec_l2_mock.assert_not_called()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.exit_code, run_test_level.EXIT_UNAVAILABLE)
        self.assertIn("SITESIFT_ADMIN_UI_ROOT", output.getvalue())

    def test_unconfigured_l2_remains_unavailable_without_dispatch(self):
        registry = {
            "levels": {
                "L2": {
                    "availability": "unconfigured",
                    "unavailableReason": "Firestore emulator suite is not configured.",
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            output = io.StringIO()
            with patch.object(run_test_level, "run_sec_l2") as run_sec_l2_mock:
                result = run_test_level.run_level(
                    "L2",
                    registry_path=registry_path,
                    output=output,
                )

        run_sec_l2_mock.assert_not_called()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.exit_code, run_test_level.EXIT_UNAVAILABLE)
        self.assertIn("Firestore emulator suite is not configured.", output.getvalue())


if __name__ == "__main__":
    unittest.main()
