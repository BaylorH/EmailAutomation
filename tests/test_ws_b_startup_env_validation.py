"""WS-B: startup env validation gate (legacy GHA 'Validate CLIENT_ID prefix').

The legacy workflow (.github/workflows/email.yml) hard-failed (exit 1) BEFORE
the pipeline ran if AZURE_API_APP_ID did not start with '54cec'. The container
runtime previously had only the in-run soft warning at main.get_graph_headers
('⚠️ Unexpected appid'), which prints and continues. Parity + fail-closed
requires a startup gate: before lease acquisition, exit non-zero when the app
id is missing or on the wrong tenant. Skipped under E2E_TEST_MODE (mock env).
"""

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import main


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOOD_APP_ID = "54cec0a2-0000-0000-0000-000000000000"


def _env(app_id=None, e2e=None):
    """Env overlay: control AZURE_API_APP_ID and E2E_TEST_MODE precisely."""
    env = dict(os.environ)
    env.pop("AZURE_API_APP_ID", None)
    env.pop("E2E_TEST_MODE", None)
    if app_id is not None:
        env["AZURE_API_APP_ID"] = app_id
    if e2e is not None:
        env["E2E_TEST_MODE"] = e2e
    return patch.dict(os.environ, env, clear=True)


class StartupEnvValidationUnitTests(unittest.TestCase):
    def test_expected_prefix_passes(self):
        with _env(app_id=GOOD_APP_ID):
            main._validate_startup_env()  # must not raise

    def test_wrong_prefix_exits_nonzero(self):
        with _env(app_id="deadbeef-1111-2222-3333-444444444444"):
            with self.assertRaises(SystemExit) as ctx:
                main._validate_startup_env()
        self.assertNotEqual(0, ctx.exception.code)
        self.assertIsNotNone(ctx.exception.code)

    def test_missing_app_id_exits_nonzero(self):
        with _env(app_id=None):
            with self.assertRaises(SystemExit) as ctx:
                main._validate_startup_env()
        self.assertNotEqual(0, ctx.exception.code)
        self.assertIsNotNone(ctx.exception.code)

    def test_e2e_test_mode_skips_gate(self):
        with _env(app_id=None, e2e="true"):
            main._validate_startup_env()  # must not raise

    def test_e2e_flag_must_be_exactly_true(self):
        """A mistyped E2E flag must not open the gate."""
        with _env(app_id=None, e2e="1"):
            with self.assertRaises(SystemExit):
                main._validate_startup_env()


class StartupEnvValidationSubprocessTests(unittest.TestCase):
    def test_python_main_dies_before_lease_on_bad_prefix(self):
        """`python main.py` with a wrong-tenant app id must exit non-zero
        BEFORE any lease acquisition or user processing output appears."""
        env = dict(os.environ)
        env.pop("E2E_TEST_MODE", None)
        env.update(
            {
                # Bad prefix under test; the rest are non-empty dummies so
                # app_config's import-time missing-env check passes and the
                # prefix gate (not the missing-env gate) is what fires.
                "AZURE_API_APP_ID": "deadbeef-1111-2222-3333-444444444444",
                "AZURE_API_CLIENT_SECRET": "dummy-secret",
                "FIREBASE_API_KEY": "dummy-firebase-key",
                "OPENAI_API_KEY": "dummy-openai-key",
                "PYTHONHASHSEED": "0",
                # Defense-in-depth: even if the startup gate regresses, the
                # subprocess must NEVER reach production Firestore. Pointing
                # the client at a dead emulator port makes any lease RPC fail
                # fast locally instead of touching live data.
                "FIRESTORE_EMULATOR_HOST": "127.0.0.1:1",
            }
        )
        proc = subprocess.run(
            [sys.executable, "main.py"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(0, proc.returncode, combined)
        self.assertIn("Startup gate", combined)
        # Nothing lease- or user-shaped may have run.
        self.assertNotIn("Scheduler lease", combined)
        self.assertNotIn("Found", combined)  # '📦 Found N token cache users'
        self.assertNotIn("Processing user", combined)


if __name__ == "__main__":
    unittest.main()
