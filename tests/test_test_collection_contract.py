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
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class TestTestCollectionContract(unittest.TestCase):
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
        with tempfile.TemporaryDirectory(prefix="pytest-collection-home-") as temp:
            temp_root = Path(temp)
            temp_home = temp_root / "empty_home"
            import_guard = temp_root / "import_guard"
            guard_ready = temp_root / "collection_guard.ready"
            guard_log = temp_root / "collection_guard.log"
            temp_home.mkdir()
            import_guard.mkdir()
            (import_guard / "sitecustomize.py").write_text(
                textwrap.dedent(
                    """
                    import atexit
                    import http.client
                    import os
                    from pathlib import Path
                    import socket
                    import sys
                    import types
                    import urllib.request

                    guard_log = Path(os.environ["COLLECTION_GUARD_LOG"])
                    module_guards = []
                    attribute_guards = []
                    protected_module_attributes = {}


                    def record(name):
                        with guard_log.open("a", encoding="utf-8") as log_file:
                            log_file.write(name + "\\n")


                    def blocked(name):
                        def fail(*args, **kwargs):
                            record("BOUNDARY_CALLED:" + name)
                            raise RuntimeError("COLLECTION_EFFECT_ATTEMPTED:" + name)
                        return fail


                    audit_state = {
                        "block_construction": False,
                        "probe_expected": None,
                        "probe_seen": [],
                    }


                    class AuditProbeBlocked(RuntimeError):
                        pass


                    def reject_socket_audit(event, args):
                        expected = audit_state["probe_expected"]
                        if expected is not None and event == expected:
                            audit_state["probe_seen"].append(event)
                            raise AuditProbeBlocked(event)
                        if event == "socket.__new__" and not audit_state["block_construction"]:
                            return
                        if event in {
                            "socket.__new__",
                            "socket.connect",
                            "socket.connect_ex",
                            "socket.getaddrinfo",
                            "socket.gethostbyname",
                            "socket.gethostbyaddr",
                            "socket.getnameinfo",
                        }:
                            record("BOUNDARY_CALLED:audit:" + event)
                            raise RuntimeError("COLLECTION_EFFECT_ATTEMPTED:audit:" + event)


                    sys.addaudithook(reject_socket_audit)

                    # Prove saved resolver callables emit an audit event before they can
                    # resolve anything. gethostbyname_ex shares socket.gethostbyname, and getfqdn
                    # reaches socket.gethostbyaddr. A missing audit event aborts sitecustomize
                    # before the ready marker, never falling through to an actual lookup.
                    resolution_audit_probes = (
                        ("socket.getaddrinfo", socket.getaddrinfo, ("audit.invalid", 443)),
                        ("socket.gethostbyname", socket.gethostbyname, ("audit.invalid",)),
                        ("socket.gethostbyname", socket.gethostbyname_ex, ("audit.invalid",)),
                        ("socket.gethostbyaddr", socket.gethostbyaddr, ("203.0.113.1",)),
                        ("socket.gethostbyaddr", socket.getfqdn, ("203.0.113.1",)),
                        ("socket.getnameinfo", socket.getnameinfo, (("203.0.113.1", 443), 0)),
                    )
                    for expected_event, resolver, resolver_args in resolution_audit_probes:
                        audit_state["probe_expected"] = expected_event
                        try:
                            resolver(*resolver_args)
                        except AuditProbeBlocked as exc:
                            if str(exc) != expected_event:
                                raise RuntimeError(
                                    "COLLECTION_AUDIT_PROBE_WRONG_EVENT:"
                                    f"{expected_event}:{exc}"
                                ) from exc
                        else:
                            raise RuntimeError(
                                "COLLECTION_AUDIT_PROBE_MISSING:" + expected_event
                            )
                    audit_state["probe_expected"] = None
                    if audit_state["probe_seen"] != [
                        probe[0] for probe in resolution_audit_probes
                    ]:
                        raise RuntimeError("COLLECTION_AUDIT_PROBE_INCOMPLETE")

                    # SDK import dependencies perform a caught IPv6-capability socket
                    # construction inside urllib3. Preload them before the collection
                    # measurement boundary while the audit hook already forbids DNS and
                    # connection attempts.
                    import firebase_admin
                    from google.cloud import firestore
                    import msal
                    import openai
                    import requests

                    try:
                        import httpx
                    except ImportError:
                        httpx = None

                    audit_state["block_construction"] = True


                    class GuardedModule(types.ModuleType):
                        def __setattr__(self, attribute, value):
                            guard = protected_module_attributes.get((id(self), attribute))
                            if guard is not None and value is not guard["value"]:
                                record("BOUNDARY_REPLACED:" + guard["name"])
                                raise RuntimeError(
                                    "COLLECTION_GUARD_REPLACED:" + guard["name"]
                                )
                            super().__setattr__(attribute, value)

                        def __delattr__(self, attribute):
                            guard = protected_module_attributes.get((id(self), attribute))
                            if guard is not None:
                                record("BOUNDARY_REPLACED:" + guard["name"])
                                raise RuntimeError(
                                    "COLLECTION_GUARD_REPLACED:" + guard["name"]
                                )
                            super().__delattr__(attribute)


                    def protect_module_attribute(module, attribute, name, value=None):
                        expected = blocked(name) if value is None else value
                        setattr(module, attribute, expected)
                        guard = {
                            "module": module,
                            "attribute": attribute,
                            "name": name,
                            "value": expected,
                        }
                        protected_module_attributes[(id(module), attribute)] = guard
                        module_guards.append(guard)
                        if module.__class__ is types.ModuleType:
                            module.__class__ = GuardedModule
                        return expected


                    def protect_attribute(owner, attribute, name):
                        expected = blocked(name)
                        setattr(owner, attribute, expected)
                        attribute_guards.append((owner, attribute, name, expected))
                        return expected


                    original_socket_type = socket.socket
                    protect_attribute(
                        original_socket_type, "connect", "socket.socket.connect"
                    )


                    class BlockedSocket(original_socket_type):
                        def __new__(cls, *args, **kwargs):
                            record("BOUNDARY_CALLED:socket.socket")
                            raise RuntimeError(
                                "COLLECTION_EFFECT_ATTEMPTED:socket.socket"
                            )


                    protect_module_attribute(
                        socket, "socket", "socket.socket", BlockedSocket
                    )
                    protect_module_attribute(
                        socket, "create_connection", "socket.create_connection"
                    )
                    for resolver_name in (
                        "getaddrinfo",
                        "gethostbyname",
                        "gethostbyname_ex",
                        "gethostbyaddr",
                        "getnameinfo",
                        "getfqdn",
                    ):
                        protect_module_attribute(
                            socket, resolver_name, "socket." + resolver_name
                        )
                    protect_module_attribute(
                        urllib.request, "urlopen", "urllib.request.urlopen"
                    )
                    protect_attribute(
                        http.client.HTTPConnection,
                        "request",
                        "http.client.HTTPConnection.request",
                    )
                    protect_attribute(
                        http.client.HTTPConnection,
                        "connect",
                        "http.client.HTTPConnection.connect",
                    )
                    protect_attribute(
                        http.client.HTTPSConnection,
                        "request",
                        "http.client.HTTPSConnection.request",
                    )
                    protect_attribute(
                        http.client.HTTPSConnection,
                        "connect",
                        "http.client.HTTPSConnection.connect",
                    )

                    protect_module_attribute(firestore, "Client", "firestore.Client")
                    protect_module_attribute(
                        firebase_admin,
                        "initialize_app",
                        "firebase_admin.initialize_app",
                    )
                    protect_module_attribute(
                        msal,
                        "PublicClientApplication",
                        "msal.PublicClientApplication",
                    )
                    protect_module_attribute(openai, "OpenAI", "openai.OpenAI")
                    protect_module_attribute(
                        requests.api, "request", "requests.api.request"
                    )
                    protect_attribute(
                        requests.sessions.Session,
                        "request",
                        "requests.sessions.Session.request",
                    )

                    if httpx is not None:
                        protect_attribute(httpx.Client, "send", "httpx.Client.send")
                        protect_attribute(
                            httpx.AsyncClient, "send", "httpx.AsyncClient.send"
                        )


                    @atexit.register
                    def assert_guard_identity():
                        for guard in module_guards:
                            current = getattr(
                                guard["module"], guard["attribute"], None
                            )
                            if current is not guard["value"]:
                                record("BOUNDARY_IDENTITY_LOST:" + guard["name"])
                        for owner, attribute, name, expected in attribute_guards:
                            if getattr(owner, attribute, None) is not expected:
                                record("BOUNDARY_IDENTITY_LOST:" + name)


                    Path(os.environ["COLLECTION_GUARD_READY"]).write_text(
                        "ready", encoding="utf-8"
                    )
                    """
                ),
                encoding="utf-8",
            )

            env = _credential_absent_env()
            env["HOME"] = str(temp_home)
            env["PYTHONPATH"] = str(import_guard)
            env["COLLECTION_GUARD_READY"] = str(guard_ready)
            env["COLLECTION_GUARD_LOG"] = str(guard_log)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--noconftest",
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

            self.assertEqual("ready", guard_ready.read_text(encoding="utf-8"))
            boundary_calls = (
                guard_log.read_text(encoding="utf-8").splitlines()
                if guard_log.exists()
                else []
            )

        output = completed.stdout + completed.stderr
        failure_tail = output[-16000:]
        self.assertEqual([], boundary_calls, failure_tail)
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
