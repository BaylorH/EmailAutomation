"""Repository-wide pytest collection must be credential-free and complete."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
DISCOVERED_LIFECYCLE_SCRIPT = REPO_ROOT / "tests" / "campaign_lifecycle_test.py"
MANUAL_LIFECYCLE_SCRIPT = REPO_ROOT / "scripts" / "campaign_lifecycle.py"

MANUAL_EXECUTABLE_MOVES = (
    ("tests/campaign_lifecycle_test.py", "scripts/campaign_lifecycle.py"),
    ("tests/e2e_test.py", "scripts/e2e.py"),
    ("tests/email_integration_test.py", "scripts/email_integration.py"),
    ("tests/full_flow_test.py", "scripts/full_flow.py"),
    ("tests/full_pipeline_test.py", "scripts/full_pipeline.py"),
    ("tests/integration_test.py", "scripts/integration.py"),
    ("tests/multi_turn_live_test.py", "scripts/multi_turn_live.py"),
    ("tests/production_test.py", "scripts/production.py"),
    ("tests/standalone_test.py", "scripts/standalone.py"),
)

CREDENTIAL_ENV_NAMES = (
    "OPENAI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_API_APP_ID",
    "AZURE_API_CLIENT_SECRET",
    "FIREBASE_API_KEY",
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
)


def _credential_absent_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in CREDENTIAL_ENV_NAMES:
        env.pop(name, None)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class TestTestCollectionContract(unittest.TestCase):
    def test_provider_client_boundary_is_collection_only_and_restored(self):
        try:
            import conftest as collection_config
        except ModuleNotFoundError:
            self.fail("root conftest.py does not define the collection-only provider boundary")

        from google.cloud import firestore

        original_client = firestore.Client
        normal_config = SimpleNamespace(option=SimpleNamespace(collectonly=False))
        collection_config.pytest_configure(normal_config)
        self.assertIs(original_client, firestore.Client)
        collection_config.pytest_unconfigure(normal_config)

        collect_config = SimpleNamespace(option=SimpleNamespace(collectonly=True))
        collection_config.pytest_configure(collect_config)
        try:
            self.assertIsNot(original_client, firestore.Client)
            self.assertIs(firestore.Client(), firestore.Client())
        finally:
            collection_config.pytest_unconfigure(collect_config)

        self.assertIs(original_client, firestore.Client)

    def test_manual_executables_are_outside_pytest_discovery(self):
        for discovered_relative, manual_relative in MANUAL_EXECUTABLE_MOVES:
            discovered_path = REPO_ROOT / discovered_relative
            manual_path = REPO_ROOT / manual_relative
            with self.subTest(discovered=discovered_relative, manual=manual_relative):
                self.assertFalse(
                    discovered_path.exists(),
                    f"manual executable remains under pytest discovery: {discovered_relative}",
                )
                self.assertTrue(
                    manual_path.is_file(),
                    f"manual executable is missing from scripts/: {manual_relative}",
                )
                self.assertFalse(manual_path.name.startswith("test"))
                self.assertFalse(manual_path.name.endswith("_test.py"))

                script_text = manual_path.read_text(encoding="utf-8")
                self.assertIn(f"python {manual_relative}", script_text)
                self.assertNotIn(f"python {discovered_relative}", script_text)

        usage_doc = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for old_command, new_command in (
            (
                "python tests/campaign_lifecycle_test.py",
                "python scripts/campaign_lifecycle.py",
            ),
            ("python tests/e2e_test.py", "python scripts/e2e.py"),
            ("python tests/standalone_test.py", "python scripts/standalone.py"),
        ):
            with self.subTest(usage_command=old_command):
                self.assertIn(new_command, usage_doc)
                self.assertNotIn(old_command, usage_doc)

    def test_manual_lifecycle_module_import_is_effect_free_without_credentials(self):
        self.assertTrue(
            MANUAL_LIFECYCLE_SCRIPT.is_file(),
            "manual lifecycle module is not yet outside pytest discovery",
        )
        import_probe = textwrap.dedent(
            f"""
            import importlib.util
            import os
            import sys
            import types

            watched_modules = (
                "google.cloud.firestore",
                "google.cloud",
                "google.oauth2.credentials",
                "google.auth.transport.requests",
                "googleapiclient.discovery",
            )
            sentinels = {{name: types.ModuleType(name) for name in watched_modules}}
            sys.modules.update(sentinels)
            before_env = dict(os.environ)

            spec = importlib.util.spec_from_file_location(
                "campaign_lifecycle_import_contract",
                {str(MANUAL_LIFECYCLE_SCRIPT)!r},
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)

            assert os.environ == before_env
            assert all(sys.modules[name] is sentinel for name, sentinel in sentinels.items())
            assert module.propose_sheet_updates is None
            print("IMPORT_OK")
            """
        )

        completed = subprocess.run(
            [sys.executable, "-c", import_probe],
            cwd=REPO_ROOT,
            env=_credential_absent_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("IMPORT_OK", completed.stdout.strip())

    def test_manual_lifecycle_cli_fails_before_provider_access_without_api_key(self):
        self.assertTrue(
            MANUAL_LIFECYCLE_SCRIPT.is_file(),
            "manual lifecycle executable is not yet available under scripts/",
        )

        with tempfile.TemporaryDirectory(prefix="campaign-lifecycle-contract-") as temp:
            temp_root = Path(temp)
            copied_scripts = temp_root / "scripts"
            import_guard = temp_root / "import_guard"
            copied_scripts.mkdir()
            import_guard.mkdir()
            copied_script = copied_scripts / MANUAL_LIFECYCLE_SCRIPT.name
            shutil.copy2(MANUAL_LIFECYCLE_SCRIPT, copied_script)
            (import_guard / "sitecustomize.py").write_text(
                textwrap.dedent(
                    """
                    import socket
                    import sys

                    class _ProviderImportBlocker:
                        def find_spec(self, fullname, path=None, target=None):
                            if fullname.split(".", 1)[0] in {
                                "firebase_admin", "google", "googleapiclient", "msal", "openai"
                            }:
                                raise RuntimeError("PROVIDER_IMPORT_ATTEMPTED:" + fullname)
                            return None

                    def _blocked_network(*args, **kwargs):
                        raise RuntimeError("NETWORK_CALL_ATTEMPTED")

                    sys.meta_path.insert(0, _ProviderImportBlocker())
                    socket.socket.connect = _blocked_network
                    socket.create_connection = _blocked_network
                    """
                ),
                encoding="utf-8",
            )

            env = _credential_absent_env()
            env["HOME"] = str(temp_root / "empty_home")
            env["PYTHONPATH"] = str(import_guard)
            completed = subprocess.run(
                [sys.executable, str(copied_script)],
                cwd=temp_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

        output = completed.stdout + completed.stderr
        self.assertEqual(1, completed.returncode, output)
        self.assertIn("OPENAI_API_KEY environment variable not set", output)
        self.assertNotIn("PROVIDER_IMPORT_ATTEMPTED", output)
        self.assertNotIn("NETWORK_CALL_ATTEMPTED", output)
        self.assertNotIn("Traceback", output)

    def test_whole_repo_collection_completes_with_credential_absent_inventory(self):
        with tempfile.TemporaryDirectory(prefix="pytest-collection-home-") as temp_home:
            env = _credential_absent_env()
            env["HOME"] = temp_home
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )

        output = completed.stdout + completed.stderr
        failure_tail = output[-16000:]
        self.assertEqual(0, completed.returncode, failure_tail)
        self.assertNotIn("INTERNALERROR", output)
        self.assertNotIn("mainloop: caught unexpected SystemExit", output)
        self.assertNotIn("INTERNALERROR> SystemExit", output)
        self.assertNotIn("OPENAI_API_KEY environment variable not set", output)

        summary = re.search(r"(\d+) tests? collected in ", output)
        self.assertIsNotNone(summary, failure_tail)
        collected_count = int(summary.group(1))
        node_ids = [line for line in completed.stdout.splitlines() if "::" in line]
        self.assertGreater(collected_count, 0)
        self.assertEqual(collected_count, len(node_ids))
        self.assertTrue(
            any(
                line.startswith("auth_service/test_auth_service_isolation.py::")
                for line in node_ids
            ),
            "collection inventory omitted auth_service tests",
        )
        self.assertTrue(
            any(
                line.startswith(
                    "tests/test_test_collection_contract.py::"
                    "TestTestCollectionContract::"
                )
                for line in node_ids
            ),
            "collection did not reach the collection contract itself",
        )


if __name__ == "__main__":
    unittest.main()
