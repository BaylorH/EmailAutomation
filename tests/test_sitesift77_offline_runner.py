import io
import os
import socket
import subprocess
import types
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tests import run_sitesift77_offline as runner


EXPECTED_MODULE_ALLOWLIST = (
    "tests.test_campaign_capabilities",
    "tests.test_recovery_payload",
    "tests.test_recovery_config",
    "tests.test_recovery_auth",
    "tests.test_recovery_dispatch",
    "tests.test_recovery_service",
    "tests.test_inbound_automation_effect_closure",
    "tests.test_outbox_safety",
    "tests.test_action_audit_backend",
    "tests.test_outbox_reply_recipient_routing",
    "tests.test_campaign_automation_pause",
    "tests.test_followup_terminal_state",
    "tests.test_pending_responses",
    "tests.test_processing_completion_guards",
    "tests.test_processing_reply_safety",
    "tests.test_jill_june_regressions",
    "tests.test_scheduler_scope",
    "tests.test_scheduler_lease",
    "tests.test_scheduler_user_listing",
    "tests.test_graph_retry_policy",
    "tests.test_graph_send_inventory",
    "tests.test_outbound_kill_switch",
    "tests.test_dead_letter_recovery",
    "tests.test_resend_failed_responses",
    "tests.test_go_condition_send_failure_observability",
    "tests.test_process_user_service",
    "tests.test_full_campaign_e2e",
)


class _SuiteLoader:
    def loadTestsFromModule(self, module):
        return module.suite


def _passing_suite():
    return unittest.TestSuite((unittest.FunctionTestCase(lambda: None),))


class OfflineEnvironmentTests(unittest.TestCase):
    def test_sanitizes_application_credentials_and_forces_closed_modes(self):
        environment = {
            name: f"secret-for-{name}"
            for name in runner.APPLICATION_CREDENTIAL_VARIABLES
        }
        environment["UNRELATED_SETTING"] = "keep-me"

        removed = runner.sanitize_environment(environment)

        self.assertEqual(set(runner.APPLICATION_CREDENTIAL_VARIABLES), set(removed))
        for name in runner.APPLICATION_CREDENTIAL_VARIABLES:
            self.assertNotIn(name, environment)
        self.assertEqual("paused", environment["SITESIFT_OUTBOUND_MODE"])
        self.assertEqual("disabled", environment["SITESIFT_RECOVERY_MODE"])
        self.assertEqual("true", environment["E2E_TEST_MODE"])
        self.assertEqual("demo-sitesift77", environment["GOOGLE_CLOUD_PROJECT"])
        self.assertEqual("demo-sitesift77", environment["GCLOUD_PROJECT"])
        self.assertEqual("demo-sitesift77", environment["FIREBASE_PROJECT_ID"])
        self.assertEqual(
            '{"projectId":"demo-sitesift77"}', environment["FIREBASE_CONFIG"]
        )
        self.assertEqual(
            "127.0.0.1:8080", environment["FIRESTORE_EMULATOR_HOST"]
        )
        self.assertEqual("keep-me", environment["UNRELATED_SETTING"])

    def test_credential_set_covers_known_application_secrets(self):
        self.assertTrue(
            {
                "OPENAI_API_KEY",
                "AZURE_API_APP_ID",
                "AZURE_API_CLIENT_SECRET",
                "AZURE_CLIENT_SECRET",
                "FIREBASE_API_KEY",
                "FIREBASE_SA_KEY",
                "FIREBASE_SERVICE_ACCOUNT_JSON",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "GOOGLE_OAUTH_CLIENT_ID",
                "GOOGLE_OAUTH_CLIENT_SECRET",
                "GOOGLE_REFRESH_TOKEN",
                "GMAIL_APP_PASSWORD",
            }.issubset(runner.APPLICATION_CREDENTIAL_VARIABLES)
        )


class LoopbackClassificationTests(unittest.TestCase):
    def test_accepts_only_localhost_and_loopback_ip_literals(self):
        for host in (
            "localhost",
            "LOCALHOST.",
            "127.0.0.1",
            "127.255.255.254",
            "::1",
            "::ffff:127.0.0.1",
            b"127.12.34.56",
        ):
            with self.subTest(host=host):
                self.assertTrue(runner.is_loopback_host(host))

        for host in (
            "example.invalid",
            "secret.example.invalid",
            "0.0.0.0",
            "10.0.0.1",
            "169.254.169.254",
            "192.0.2.9",
            "2001:db8::1",
            "",
            None,
        ):
            with self.subTest(host=host):
                self.assertFalse(runner.is_loopback_host(host))


class NetworkTripwireTests(unittest.TestCase):
    def test_socket_connections_allow_loopback_and_unix_but_block_external(self):
        original_result = object()
        with patch.object(
            socket.socket,
            "connect",
            autospec=True,
            return_value=original_result,
        ) as original_connect:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                ipv4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ipv6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(ipv4.close)
                self.addCleanup(ipv6.close)
                self.addCleanup(unix.close)

                self.assertIs(original_result, ipv4.connect(("127.9.8.7", 443)))
                self.assertIs(original_result, ipv6.connect(("::1", 443)))
                self.assertIs(original_result, unix.connect("/tmp/sitesift77.sock"))

                secret_endpoint = "secret.example.invalid"
                with self.assertRaises(runner.OfflineNetworkAccessBlocked) as caught:
                    ipv4.connect((secret_endpoint, 443))

            self.assertEqual(1, tripwire.activation_count)
            self.assertEqual({"activation_count": 1}, tripwire.summary())
            self.assertNotIn(secret_endpoint, str(caught.exception))
            self.assertNotIn("secret", repr(tripwire.summary()).lower())
            self.assertEqual(3, original_connect.call_count)

    def test_connect_ex_and_datagram_sendto_cannot_bypass_tripwire(self):
        with patch.object(
            socket.socket, "connect_ex", autospec=True, return_value=0
        ) as original_connect_ex, patch.object(
            socket.socket, "sendto", autospec=True, return_value=7
        ) as original_sendto:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.addCleanup(datagram.close)
                self.assertEqual(0, datagram.connect_ex(("127.0.0.9", 443)))
                self.assertEqual(7, datagram.sendto(b"offline", ("127.0.0.1", 9)))

                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    datagram.connect_ex(("192.0.2.55", 443))
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    datagram.sendto(b"blocked", ("198.51.100.55", 9))

            self.assertEqual(2, tripwire.activation_count)
            original_connect_ex.assert_called_once()
            original_sendto.assert_called_once()

    def test_create_connection_cannot_bypass_tripwire(self):
        result = object()
        with patch.object(socket, "create_connection", return_value=result) as original:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                self.assertIs(
                    result,
                    socket.create_connection(("::ffff:127.0.0.1", 443)),
                )
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    socket.create_connection(("203.0.113.8", 443))

            self.assertEqual(1, tripwire.activation_count)
            original.assert_called_once_with(("::ffff:127.0.0.1", 443))

    def test_bind_allows_loopback_and_unix_but_rejects_non_loopback(self):
        with patch.object(
            socket.socket, "bind", autospec=True, return_value=None
        ) as original_bind:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                ipv4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(ipv4.close)
                self.addCleanup(unix.close)
                ipv4.bind(("127.0.0.1", 0))
                unix.bind("/tmp/sitesift77-bind.sock")
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    ipv4.bind(("0.0.0.0", 0))

            self.assertEqual(1, tripwire.activation_count)
            self.assertEqual(2, original_bind.call_count)

    def test_subprocess_system_and_exec_bypasses_are_denied(self):
        with patch.object(subprocess, "Popen") as original_popen, patch.object(
            os, "system"
        ) as original_system, patch.object(os, "execv") as original_execv:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    subprocess.Popen(["secret-child", "--network"])
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    os.system("secret-command")
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    os.execv("/secret/binary", ["secret-binary"])

            self.assertEqual(3, tripwire.activation_count)
            original_popen.assert_not_called()
            original_system.assert_not_called()
            original_execv.assert_not_called()
            self.assertNotIn("secret", str(tripwire.summary()).lower())

    def test_broader_os_exec_spawn_and_fork_family_is_enumerated_and_denied(self):
        expected = (
            "system",
            "popen",
            "fork",
            "forkpty",
            "posix_spawn",
            "posix_spawnp",
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
        )
        self.assertEqual(expected, runner.BLOCKED_OS_PROCESS_FUNCTIONS)

        available = [name for name in expected if hasattr(os, name)]
        with ExitStack() as stack:
            originals = {
                name: stack.enter_context(patch.object(os, name)) for name in available
            }
            tripwire = runner.NetworkTripwire()
            with tripwire:
                for name in available:
                    with self.subTest(name=name):
                        with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                            getattr(os, name)()

            self.assertEqual(len(available), tripwire.activation_count)
            for original in originals.values():
                original.assert_not_called()

    def test_dns_allows_loopback_without_resolving_external_hosts(self):
        local_answer = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with patch.object(socket, "getaddrinfo", return_value=local_answer) as resolver:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                self.assertEqual(
                    local_answer,
                    socket.getaddrinfo("localhost", 443, type=socket.SOCK_STREAM),
                )
                self.assertEqual(
                    local_answer,
                    socket.getaddrinfo("127.0.0.1", 443, type=socket.SOCK_STREAM),
                )
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    socket.getaddrinfo("api.example.invalid", 443)

            self.assertEqual(1, tripwire.activation_count)
            self.assertEqual(2, resolver.call_count)
            resolved_hosts = [call.args[0] for call in resolver.call_args_list]
            self.assertEqual(["127.0.0.1", "127.0.0.1"], resolved_hosts)

    def test_legacy_dns_helpers_cannot_resolve_external_hosts(self):
        with patch.object(
            socket, "gethostbyname", return_value="127.0.0.1"
        ) as by_name, patch.object(
            socket,
            "gethostbyname_ex",
            return_value=("localhost", [], ["127.0.0.1"]),
        ) as by_name_ex:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                self.assertEqual("127.0.0.1", socket.gethostbyname("localhost"))
                self.assertEqual(
                    ("localhost", [], ["127.0.0.1"]),
                    socket.gethostbyname_ex("127.0.0.1"),
                )
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    socket.gethostbyname("api.example.invalid")
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    socket.gethostbyname_ex("api.example.invalid")

            self.assertEqual(2, tripwire.activation_count)
            by_name.assert_called_once_with("127.0.0.1")
            by_name_ex.assert_called_once_with("127.0.0.1")

    def test_reverse_dns_helpers_cannot_resolve_external_addresses(self):
        with patch.object(
            socket,
            "gethostbyaddr",
            return_value=("localhost", [], ["127.0.0.1"]),
        ) as by_address, patch.object(
            socket, "getnameinfo", return_value=("127.0.0.1", "443")
        ) as name_info:
            tripwire = runner.NetworkTripwire()
            with tripwire:
                self.assertEqual(
                    ("localhost", [], ["127.0.0.1"]),
                    socket.gethostbyaddr("127.0.0.1"),
                )
                self.assertEqual(
                    ("127.0.0.1", "443"),
                    socket.getnameinfo(("127.0.0.1", 443), 0),
                )
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    socket.gethostbyaddr("203.0.113.90")
                with self.assertRaises(runner.OfflineNetworkAccessBlocked):
                    socket.getnameinfo(("198.51.100.90", 443), 0)

            self.assertEqual(2, tripwire.activation_count)
            by_address.assert_not_called()
            name_info.assert_called_once_with(
                ("127.0.0.1", 443), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV
            )


class LiteralAllowlistTests(unittest.TestCase):
    def test_literal_module_allowlist_is_exact_and_ordered(self):
        self.assertIsInstance(runner.TEST_MODULE_ALLOWLIST, tuple)
        self.assertEqual(EXPECTED_MODULE_ALLOWLIST, runner.TEST_MODULE_ALLOWLIST)

    def test_builder_imports_and_loads_only_the_literal_allowlist(self):
        calls = []

        def importer(name):
            calls.append(name)
            return types.SimpleNamespace(__name__=name, suite=_passing_suite())

        suite, failures = runner.build_allowlisted_suite(
            import_module=importer,
            loader=_SuiteLoader(),
        )

        self.assertEqual([], failures)
        self.assertEqual(list(EXPECTED_MODULE_ALLOWLIST), calls)
        self.assertEqual(len(EXPECTED_MODULE_ALLOWLIST), suite.countTestCases())

    def test_import_failure_is_bounded_and_does_not_include_exception_text(self):
        calls = []

        def importer(name):
            calls.append(name)
            if name == EXPECTED_MODULE_ALLOWLIST[2]:
                raise ImportError("credential=super-secret-value")
            return types.SimpleNamespace(__name__=name, suite=_passing_suite())

        suite, failures = runner.build_allowlisted_suite(
            import_module=importer,
            loader=_SuiteLoader(),
        )

        self.assertEqual(list(EXPECTED_MODULE_ALLOWLIST), calls)
        self.assertEqual(len(EXPECTED_MODULE_ALLOWLIST) - 1, suite.countTestCases())
        self.assertEqual(1, len(failures))
        self.assertEqual(EXPECTED_MODULE_ALLOWLIST[2], failures[0].module_name)
        self.assertEqual("ImportError", failures[0].error_type)
        self.assertNotIn("super-secret-value", repr(failures[0]))

    def test_every_allowlisted_module_must_contribute_at_least_one_test(self):
        empty_name = EXPECTED_MODULE_ALLOWLIST[4]

        def importer(name):
            suite = unittest.TestSuite() if name == empty_name else _passing_suite()
            return types.SimpleNamespace(__name__=name, suite=suite)

        suite, failures = runner.build_allowlisted_suite(
            import_module=importer,
            loader=_SuiteLoader(),
        )

        self.assertEqual(len(EXPECTED_MODULE_ALLOWLIST) - 1, suite.countTestCases())
        self.assertEqual(1, len(failures))
        self.assertEqual(empty_name, failures[0].module_name)
        self.assertEqual("EmptyTestModule", failures[0].error_type)


class ExitStatusTests(unittest.TestCase):
    def _result(self, *, failures=0, errors=0, skips=0, tests_run=1):
        result = unittest.TestResult()
        result.testsRun = tests_run
        result.failures = [(object(), "failure")] * failures
        result.errors = [(object(), "error")] * errors
        result.skipped = [(object(), "skip")] * skips
        return result

    def test_zero_requires_tests_and_no_failure_error_skip_import_or_tripwire(self):
        self.assertEqual(
            0,
            runner.exit_status(
                self._result(), import_failures=(), tripwire_activation_count=0
            ),
        )

    def test_nonzero_for_test_failure_error_or_unexpected_skip(self):
        for result in (
            self._result(failures=1),
            self._result(errors=1),
            self._result(skips=1),
        ):
            with self.subTest(result=result):
                self.assertEqual(
                    1,
                    runner.exit_status(
                        result, import_failures=(), tripwire_activation_count=0
                    ),
                )

    def test_nonzero_for_expected_failure_or_unexpected_success(self):
        expected_failure = self._result()
        expected_failure.expectedFailures = [(object(), "expected failure")]
        unexpected_success = self._result()
        unexpected_success.unexpectedSuccesses = [object()]

        for result in (expected_failure, unexpected_success):
            with self.subTest(result=result):
                self.assertEqual(
                    1,
                    runner.exit_status(
                        result, import_failures=(), tripwire_activation_count=0
                    ),
                )

    def test_nonzero_for_import_failure_tripwire_activation_or_empty_suite(self):
        import_failure = runner.ImportFailure("tests.synthetic", "ImportError")
        cases = (
            (self._result(), (import_failure,), 0),
            (self._result(), (), 1),
            (self._result(tests_run=0), (), 0),
        )
        for result, import_failures, activation_count in cases:
            with self.subTest(
                import_failures=import_failures,
                activation_count=activation_count,
                tests_run=result.testsRun,
            ):
                self.assertEqual(
                    1,
                    runner.exit_status(
                        result,
                        import_failures=import_failures,
                        tripwire_activation_count=activation_count,
                    ),
                )

    def test_runner_returns_exit_status_and_prints_counts_only(self):
        environment = {
            "OPENAI_API_KEY": "top-secret-api-key",
            "SITESIFT_OUTBOUND_MODE": "live",
            "SITESIFT_RECOVERY_MODE": "enabled",
        }
        output = io.StringIO()

        def importer(name):
            return types.SimpleNamespace(__name__=name, suite=_passing_suite())

        status = runner.run_offline_tests(
            environ=environment,
            import_module=importer,
            loader=_SuiteLoader(),
            stream=output,
        )

        self.assertEqual(0, status)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertEqual("paused", environment["SITESIFT_OUTBOUND_MODE"])
        self.assertEqual("disabled", environment["SITESIFT_RECOVERY_MODE"])
        self.assertIn(f"tests={len(EXPECTED_MODULE_ALLOWLIST)}", output.getvalue())
        self.assertIn("network_tripwire_activations=0", output.getvalue())
        self.assertNotIn("top-secret-api-key", output.getvalue())

    def test_caught_tripwire_violation_still_makes_run_nonzero(self):
        output = io.StringIO()

        def caught_violation():
            try:
                socket.getaddrinfo("caught.example.invalid", 443)
            except runner.OfflineNetworkAccessBlocked:
                pass

        def importer(name):
            test = caught_violation if name == EXPECTED_MODULE_ALLOWLIST[0] else lambda: None
            return types.SimpleNamespace(
                __name__=name,
                suite=unittest.TestSuite((unittest.FunctionTestCase(test),)),
            )

        status = runner.run_offline_tests(
            environ={},
            import_module=importer,
            loader=_SuiteLoader(),
            stream=output,
        )

        self.assertEqual(1, status)
        self.assertIn("network_tripwire_activations=1", output.getvalue())


class ImportBehaviorTests(unittest.TestCase):
    def test_import_does_not_execute_the_allowlisted_suite(self):
        self.assertTrue(callable(runner.main))
        self.assertEqual(0, runner.suite_execution_count())


if __name__ == "__main__":
    unittest.main()
