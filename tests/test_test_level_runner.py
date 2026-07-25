import io
import json
import os
import socket
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
            network_is_blocked = isinstance(socket.create_connection, Mock)
            if network_is_blocked:
                try:
                    socket.create_connection(("127.0.0.1", 1))
                except run_test_level.L1NetworkAccessBlocked:
                    pass
                else:
                    network_is_blocked = False

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
