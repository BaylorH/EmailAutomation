import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import firebase_admin
import msal
import openai
from firebase_admin import firestore as admin_firestore
from google.cloud import firestore

from scripts import run_test_level


class TestCredentialFreeL1Runner(unittest.TestCase):
    def test_existing_legacy_manual_surfaces_are_explicitly_inventoried(self):
        test_root = run_test_level.REPO_ROOT / "tests"
        legacy_surfaces = frozenset(
            path.relative_to(test_root).as_posix()
            for path in test_root.rglob("*_test.py")
            if path.is_file() and not path.name.startswith("test")
        )

        self.assertEqual(
            legacy_surfaces,
            run_test_level.L1_EXCLUDED_MANUAL_SURFACES,
        )

    def test_unregistered_legacy_surface_fails_closed_before_import(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_root = Path(tmp_dir)
            import_marker = test_root / "legacy-imported"
            (test_root / "new_manual_test.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(import_marker)!r}).write_text('imported')\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "unregistered legacy manual test surface.*new_manual_test.py",
            ):
                run_test_level._discover_l1_suite(
                    test_root=test_root,
                    excluded_manual_surfaces=frozenset(),
                )

            self.assertFalse(import_marker.exists())

    def test_allowlisted_legacy_surface_is_not_collected_by_real_discovery(self):
        original_sys_path = sys.path.copy()
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                test_root = Path(tmp_dir)
                import_marker = test_root / "legacy-imported"
                (test_root / "manual_flow_test.py").write_text(
                    "from pathlib import Path\n"
                    f"Path({str(import_marker)!r}).write_text('imported')\n"
                    "raise AssertionError('manual surface must not be imported')\n",
                    encoding="utf-8",
                )

                suite = run_test_level._discover_l1_suite(
                    test_root=test_root,
                    excluded_manual_surfaces=frozenset({"manual_flow_test.py"}),
                )

                self.assertEqual(suite.countTestCases(), 0)
                self.assertFalse(import_marker.exists())
        finally:
            sys.path[:] = original_sys_path

    def test_canonical_rename_opts_manual_surface_into_l1_boundary(self):
        original_sys_path = sys.path.copy()
        module_name = "test_manual_boundary"
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                test_root = Path(tmp_dir)
                manual_path = test_root / "manual_boundary_test.py"
                canonical_path = test_root / f"{module_name}.py"
                manual_path.write_text(
                    "import os\n"
                    "import socket\n"
                    "import unittest\n"
                    "from scripts import run_test_level\n"
                    "\n"
                    "DISCOVERY_WAS_CREDENTIAL_FREE = (\n"
                    "    'OPENAI_API_KEY' not in os.environ\n"
                    "    and os.environ.get('E2E_TEST_MODE') == 'true'\n"
                    ")\n"
                    "try:\n"
                    "    socket.gethostbyname('example.invalid')\n"
                    "except run_test_level.L1NetworkAccessBlocked:\n"
                    "    DISCOVERY_NETWORK_WAS_BLOCKED = True\n"
                    "else:\n"
                    "    DISCOVERY_NETWORK_WAS_BLOCKED = False\n"
                    "\n"
                    "class CanonicalBoundaryCase(unittest.TestCase):\n"
                    "    def test_discovery_and_execution_use_l1_boundary(self):\n"
                    "        self.assertTrue(DISCOVERY_WAS_CREDENTIAL_FREE)\n"
                    "        self.assertTrue(DISCOVERY_NETWORK_WAS_BLOCKED)\n"
                    "        self.assertNotIn('OPENAI_API_KEY', os.environ)\n"
                    "        with self.assertRaises(\n"
                    "            run_test_level.L1NetworkAccessBlocked\n"
                    "        ):\n"
                    "            socket.gethostbyname('example.invalid')\n",
                    encoding="utf-8",
                )

                excluded_suite = run_test_level._discover_l1_suite(
                    test_root=test_root,
                    excluded_manual_surfaces=frozenset(
                        {"manual_boundary_test.py"}
                    ),
                )
                self.assertEqual(excluded_suite.countTestCases(), 0)

                manual_path.rename(canonical_path)
                output = io.StringIO()
                with patch.dict(
                    os.environ,
                    {"OPENAI_API_KEY": "live-key"},
                ):
                    result = run_test_level.run_l1(
                        suite_factory=lambda: run_test_level._discover_l1_suite(
                            test_root=test_root,
                            excluded_manual_surfaces=frozenset(),
                        ),
                        output=output,
                    )

                self.assertEqual(result.status, "passed", result.detail)
                self.assertEqual(result.tests_run, 1)
        finally:
            sys.modules.pop(module_name, None)
            sys.path[:] = original_sys_path

    def test_l1_bootstrap_covers_discovery_and_execution(self):
        observations = []
        sensitive_environment = {
            "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/does-not-exist.json",
            "GOOGLE_REFRESH_TOKEN": "live-google-token",
            "OPENAI_API_KEY": "live-openai-key",
            "AZURE_API_APP_ID": "live-azure-client",
            "AZURE_API_CLIENT_SECRET": "live-azure-secret",
            "FIREBASE_SERVICE_ACCOUNT_JSON": "live-firebase-json",
        }

        def observe_boundary(phase):
            dns_functions = (
                socket.getaddrinfo,
                socket.getfqdn,
                socket.gethostbyaddr,
                socket.gethostbyname,
                socket.gethostbyname_ex,
                socket.getnameinfo,
            )
            required_socket_overrides = {
                "connect",
                "connect_ex",
                "sendmsg",
                "sendto",
            }
            network_is_blocked = (
                isinstance(socket.create_connection, Mock)
                and all(isinstance(function, Mock) for function in dns_functions)
                and socket.socket is run_test_level._CredentialFreeSocket
                and required_socket_overrides <= set(socket.socket.__dict__)
            )
            if network_is_blocked:
                guarded_calls = (
                    lambda: socket.create_connection(("127.0.0.1", 1)),
                    lambda: socket.gethostbyname("example.com"),
                    lambda: socket.socket(
                        socket.AF_INET,
                        socket.SOCK_DGRAM,
                    ).sendto(b"probe", ("127.0.0.1", 9)),
                )
                for guarded_call in guarded_calls:
                    try:
                        guarded_call()
                    except run_test_level.L1NetworkAccessBlocked:
                        continue
                    network_is_blocked = False
                    break

            observations.append(
                (
                    phase,
                    all(name not in os.environ for name in sensitive_environment),
                    os.environ.get("E2E_TEST_MODE"),
                    isinstance(firestore.Client, Mock),
                    isinstance(admin_firestore.client, Mock),
                    isinstance(firebase_admin.initialize_app, Mock),
                    isinstance(msal.PublicClientApplication, Mock),
                    isinstance(msal.ConfidentialClientApplication, Mock),
                    isinstance(openai.OpenAI, Mock),
                    network_is_blocked,
                )
            )

        class BootstrapCase(unittest.TestCase):
            def runTest(self):
                observe_boundary("execution")
                os.environ["CREATED_DURING_L1_TOKEN"] = "synthetic"

        def suite_factory():
            observe_boundary("discovery")
            return unittest.TestSuite([BootstrapCase()])

        output = io.StringIO()
        with unittest.mock.patch.dict(
            os.environ,
            {
                **sensitive_environment,
                "E2E_TEST_MODE": "previous-value",
            },
        ):
            result = run_test_level.run_l1(
                suite_factory=suite_factory,
                output=output,
            )
            for name, value in sensitive_environment.items():
                self.assertEqual(os.environ[name], value)
            self.assertEqual(os.environ["E2E_TEST_MODE"], "previous-value")
            self.assertNotIn("CREATED_DURING_L1_TOKEN", os.environ)

        self.assertEqual(
            observations,
            [
                ("discovery", True, "true", True, True, True, True, True, True, True),
                ("execution", True, "true", True, True, True, True, True, True, True),
            ],
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.tests_run, 1)
        self.assertEqual(result.exit_code, run_test_level.EXIT_PASSED)
        self.assertIn("L1 PASSED tests=1", output.getvalue())

    def test_unavailable_level_is_not_an_assertion_failure(self):
        registry = {
            "levels": {
                "L2": {
                    "availability": "unconfigured",
                    "unavailableReason": "Firestore emulator suite is not configured.",
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "scenario-registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            output = io.StringIO()

            result = run_test_level.run_level(
                "L2",
                registry_path=registry_path,
                output=output,
            )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.exit_code, run_test_level.EXIT_UNAVAILABLE)
        self.assertNotEqual(result.exit_code, run_test_level.EXIT_FAILED)
        self.assertIn("L2 UNAVAILABLE", output.getvalue())
        self.assertIn("Firestore emulator suite is not configured.", output.getvalue())

    def test_failed_test_uses_failure_exit_code(self):
        class FailingCase(unittest.TestCase):
            def runTest(self):
                self.fail("intentional failure")

        result = run_test_level.run_l1(
            suite_factory=lambda: unittest.TestSuite([FailingCase()]),
            output=io.StringIO(),
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, run_test_level.EXIT_FAILED)
        self.assertNotEqual(result.exit_code, run_test_level.EXIT_UNAVAILABLE)

    def test_missing_l1_dependency_is_unavailable_before_discovery(self):
        registry = {
            "levels": {
                "L1": {
                    "requiredPythonModules": ["required_test_dependency"],
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            registry_path = Path(tmp_dir) / "scenario-registry.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            output = io.StringIO()

            with patch.object(
                run_test_level.importlib.util,
                "find_spec",
                return_value=None,
            ), patch.object(run_test_level, "run_l1") as run_l1:
                result = run_test_level.run_level(
                    "L1",
                    registry_path=registry_path,
                    output=output,
                )

        run_l1.assert_not_called()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.exit_code, run_test_level.EXIT_UNAVAILABLE)
        self.assertIn("required_test_dependency", output.getvalue())


if __name__ == "__main__":
    unittest.main()
