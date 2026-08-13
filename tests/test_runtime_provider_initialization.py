from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_credential_free_health_probe():
    env = os.environ.copy()
    for name in (
        "OPENAI_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_API_APP_ID",
        "AZURE_API_CLIENT_SECRET",
        "FIREBASE_API_KEY",
        "GMAIL_ADDRESS",
        "GMAIL_APP_PASSWORD",
    ):
        env.pop(name, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTEST_ADDOPTS", None)
    env["E2E_TEST_MODE"] = "true"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    source = textwrap.dedent("""
        import http.client
        import socket
        import sys
        import urllib.request
        from unittest.mock import patch

        calls = []

        def blocked(name):
            def fail(*args, **kwargs):
                calls.append(name)
                raise AssertionError("offline boundary called: " + name)
            return fail

        audit_state = {"block_construction": False}

        def reject_socket_audit(event, args):
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
                calls.append("audit:" + event)
                raise AssertionError("offline boundary called: audit:" + event)

        sys.addaudithook(reject_socket_audit)

        # Preload SDK dependencies before measuring service import. The early
        # audit hook already forbids DNS and connection attempts; only urllib3's
        # caught local IPv6 capability socket construction is excluded.
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

        original_socket_type = socket.socket

        class BlockedSocket(original_socket_type):
            def __new__(cls, *args, **kwargs):
                calls.append("socket.socket")
                raise AssertionError("offline boundary called: socket.socket")

        transport_patches = [
            patch.object(original_socket_type, "connect", blocked("socket.socket.connect")),
            patch.object(socket, "socket", BlockedSocket),
            patch.object(socket, "create_connection", blocked("socket.create_connection")),
            patch.object(socket, "getaddrinfo", blocked("socket.getaddrinfo")),
            patch.object(socket, "gethostbyname", blocked("socket.gethostbyname")),
            patch.object(socket, "gethostbyname_ex", blocked("socket.gethostbyname_ex")),
            patch.object(socket, "gethostbyaddr", blocked("socket.gethostbyaddr")),
            patch.object(socket, "getnameinfo", blocked("socket.getnameinfo")),
            patch.object(socket, "getfqdn", blocked("socket.getfqdn")),
            patch.object(urllib.request, "urlopen", blocked("urllib.request.urlopen")),
            patch.object(http.client.HTTPConnection, "request", blocked("http.client.HTTPConnection.request")),
            patch.object(http.client.HTTPConnection, "connect", blocked("http.client.HTTPConnection.connect")),
            patch.object(http.client.HTTPSConnection, "request", blocked("http.client.HTTPSConnection.request")),
            patch.object(http.client.HTTPSConnection, "connect", blocked("http.client.HTTPSConnection.connect")),
        ]
        provider_patches = [
            patch.object(firestore, "Client", blocked("firestore.Client")),
            patch.object(firebase_admin, "initialize_app", blocked("firebase_admin.initialize_app")),
            patch.object(msal, "PublicClientApplication", blocked("msal.PublicClientApplication")),
            patch.object(openai, "OpenAI", blocked("openai.OpenAI")),
            patch.object(requests.api, "request", blocked("requests.api.request")),
            patch.object(requests.sessions.Session, "request", blocked("requests.sessions.Session.request")),
        ]
        if httpx is not None:
            provider_patches.extend([
                patch.object(httpx.Client, "send", blocked("httpx.Client.send")),
                patch.object(httpx.AsyncClient, "send", blocked("httpx.AsyncClient.send")),
            ])
        started = []
        try:
            for boundary_patch in transport_patches + provider_patches:
                boundary_patch.start()
                started.append(boundary_patch)

            import service
            with service.app.test_client() as client:
                health = client.get("/health")
                healthz = client.get("/healthz")
            assert health.status_code == 200
            assert health.get_json() == {"status": "ok"}
            assert healthz.status_code == 200
            assert healthz.get_json() == {"status": "ok"}
            assert calls == []
        finally:
            for boundary_patch in reversed(started):
                boundary_patch.stop()
        print("HEALTH_OFFLINE_OK")
    """)
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("health probe exited 0 without a marker")
    return lines[-1]


CREDENTIAL_ENV_NAMES = (
    "OPENAI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_API_APP_ID",
    "AZURE_API_CLIENT_SECRET",
    "FIREBASE_API_KEY",
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
)


def _probe_env(*, e2e=False):
    env = os.environ.copy()
    for name in CREDENTIAL_ENV_NAMES:
        env.pop(name, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if e2e:
        env["E2E_TEST_MODE"] = "true"
    else:
        env.pop("E2E_TEST_MODE", None)
    return env


def _run_probe(source, *, e2e=False):
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=REPO_ROOT,
        env=_probe_env(e2e=e2e),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError("probe exited 0 without a marker")
    return lines[-1]


class RuntimeProviderInitializationTests(unittest.TestCase):
    def test_health_import_constructs_no_provider_socket_or_http_client(self):
        self.assertEqual("HEALTH_OFFLINE_OK", _run_credential_free_health_probe())

    def test_clients_import_constructs_nothing(self):
        self.assertEqual("IMPORT_OK", _run_probe("""
            from unittest.mock import patch
            from google.cloud import firestore
            import openai

            def blocked(name):
                def fail(*args, **kwargs):
                    raise AssertionError(name)
                return fail

            with patch.object(firestore, "Client", blocked("firestore.Client")), \
                 patch.object(openai, "OpenAI", blocked("openai.OpenAI")):
                import email_automation.clients
            print("IMPORT_OK")
        """, e2e=True))

    def test_scheduler_runner_import_constructs_nothing_without_credentials(self):
        self.assertEqual("IMPORT_OK", _run_probe("""
            from unittest.mock import patch
            from google.cloud import firestore
            import openai

            def blocked(name):
                def fail(*args, **kwargs):
                    raise AssertionError(name)
                return fail

            with patch.object(firestore, "Client", blocked("firestore.Client")), \
                 patch.object(openai, "OpenAI", blocked("openai.OpenAI")):
                import scheduler_runner
            print("IMPORT_OK")
        """))

    def test_scheduler_first_runtime_entry_fails_before_http_without_config(self):
        self.assertEqual("CONFIG_FAIL_FAST_OK", _run_probe("""
            from unittest.mock import patch
            import scheduler_runner as scheduler

            with patch.object(
                scheduler.requests,
                "get",
                side_effect=AssertionError("HTTP reached before config validation"),
            ):
                try:
                    scheduler.list_user_ids()
                except RuntimeError as exc:
                    assert str(exc) == "Missing required env vars"
                else:
                    raise AssertionError("missing config did not fail closed")
            print("CONFIG_FAIL_FAST_OK")
        """))

    def test_clients_firestore_and_openai_retry_after_first_constructor_failure(self):
        self.assertEqual("RETRY_OK", _run_probe("""
            from types import SimpleNamespace
            from unittest.mock import patch
            from google.cloud import firestore
            import openai

            fake_fs = SimpleNamespace(collection=lambda name: ("collection", name))
            fake_ai = SimpleNamespace(responses=object())
            with patch.object(
                firestore, "Client", side_effect=[RuntimeError("fs-first"), fake_fs]
            ) as fs_ctor, patch.object(
                openai, "OpenAI", side_effect=[RuntimeError("ai-first"), fake_ai]
            ) as ai_ctor:
                import email_automation.clients as clients
                try:
                    clients._fs.collection("users")
                except RuntimeError as exc:
                    assert str(exc) == "fs-first"
                else:
                    raise AssertionError("first Firestore construction did not fail")
                assert clients._fs.initialized is False
                assert clients._fs.collection("users") == ("collection", "users")

                try:
                    clients.client.responses
                except RuntimeError as exc:
                    assert str(exc) == "ai-first"
                else:
                    raise AssertionError("first OpenAI construction did not fail")
                assert clients.client.initialized is False
                assert clients.client.responses is fake_ai.responses
                assert fs_ctor.call_count == 2
                assert ai_ctor.call_count == 2
            print("RETRY_OK")
        """, e2e=True))

    def test_scheduler_firestore_and_openai_retry_after_first_constructor_failure(self):
        self.assertEqual("RETRY_OK", _run_probe("""
            import os
            from types import SimpleNamespace
            from unittest.mock import patch
            from google.cloud import firestore
            import openai

            os.environ.update({
                "AZURE_API_APP_ID": "test-client",
                "AZURE_API_CLIENT_SECRET": "test-secret",
                "FIREBASE_API_KEY": "test-firebase",
                "OPENAI_API_KEY": "test-openai",
            })
            fake_fs = SimpleNamespace(collection=lambda name: ("collection", name))
            fake_ai = SimpleNamespace(responses=object())
            with patch.object(
                firestore, "Client", side_effect=[RuntimeError("fs-first"), fake_fs]
            ) as fs_ctor, patch.object(
                openai, "OpenAI", side_effect=[RuntimeError("ai-first"), fake_ai]
            ) as ai_ctor:
                import scheduler_runner as scheduler
                for proxy, attribute, message in (
                    (scheduler._fs, lambda: scheduler._fs.collection("users"), "fs-first"),
                    (scheduler.client, lambda: scheduler.client.responses, "ai-first"),
                ):
                    try:
                        attribute()
                    except RuntimeError as exc:
                        assert str(exc) == message
                    else:
                        raise AssertionError(message + " was not raised")
                    assert proxy.initialized is False
                    attribute()
                assert fs_ctor.call_count == 2
                assert ai_ctor.call_count == 2
            print("RETRY_OK")
        """))

    def test_clients_concurrent_first_use_constructs_each_provider_once(self):
        self.assertEqual("CONCURRENCY_OK", _run_probe("""
            import threading
            import time
            from concurrent.futures import ThreadPoolExecutor
            from types import SimpleNamespace
            from unittest.mock import patch
            from google.cloud import firestore
            import openai

            workers = 16
            fs_calls = 0
            ai_calls = 0
            count_lock = threading.Lock()
            fake_fs = SimpleNamespace(collection=lambda name: name)
            fake_ai = SimpleNamespace(responses=object())

            def make_fs():
                global fs_calls
                with count_lock:
                    fs_calls += 1
                time.sleep(0.03)
                return fake_fs

            def make_ai(*, api_key):
                global ai_calls
                assert api_key
                with count_lock:
                    ai_calls += 1
                time.sleep(0.03)
                return fake_ai

            with patch.object(firestore, "Client", side_effect=make_fs), \
                 patch.object(openai, "OpenAI", side_effect=make_ai):
                import email_automation.clients as clients
                assert fs_calls == 0
                assert ai_calls == 0
                for read in (
                    lambda: clients._fs.collection("users"),
                    lambda: clients.client.responses,
                ):
                    barrier = threading.Barrier(workers + 1)
                    def worker():
                        barrier.wait(timeout=5)
                        return read()
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        futures = [pool.submit(worker) for _ in range(workers)]
                        barrier.wait(timeout=5)
                        [future.result(timeout=5) for future in futures]
                assert fs_calls == 1
                assert ai_calls == 1
            print("CONCURRENCY_OK")
        """, e2e=True))
