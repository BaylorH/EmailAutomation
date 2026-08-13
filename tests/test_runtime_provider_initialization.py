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


class RuntimeProviderInitializationTests(unittest.TestCase):
    def test_health_import_constructs_no_provider_socket_or_http_client(self):
        self.assertEqual("HEALTH_OFFLINE_OK", _run_credential_free_health_probe())
