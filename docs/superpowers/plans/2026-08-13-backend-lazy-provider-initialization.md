# Backend Lazy Provider Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all repository test collection credential-free and zero-constructor by moving Firestore, Firebase Admin, OpenAI, and the auth service's legacy MSAL construction to their first real runtime use without changing live provider semantics.

**Architecture:** A dependency-free, thread-safe `LazyProviderProxy` preserves the compatibility-sensitive `_fs` and `client` module globals. The Flask authentication boundaries use explicit lock-backed Firebase getters, while the standalone auth service builds its legacy MSAL fallback only for a legacy pending-flow record; pytest no longer substitutes production constructors during collection.

**Tech Stack:** Python 3.12+, `threading.Lock`, Firebase Admin SDK, Google Cloud Firestore, OpenAI Python SDK, MSAL, Flask, `unittest.mock`, pytest subprocess collection.

**Deliverable:** finding — this branch contains only the reviewed design and executable implementation plan; a later authorized execution produces code and tests.

**Design:** `docs/superpowers/specs/2026-08-13-backend-lazy-provider-initialization-design.md`

---

## File structure

- Create `email_automation/lazy_provider.py`: pure one-process lazy proxy; no provider imports.
- Create `tests/test_lazy_provider.py`: concurrency, failure retry, and no-introspection-initialization contract.
- Create `tests/test_runtime_provider_initialization.py`: fresh-child import, first-use, health, Firebase, and MSAL startup regressions.
- Modify `email_automation/clients.py`: lazy Firestore/OpenAI globals.
- Modify `scheduler_runner.py`: lazy Firestore/OpenAI globals and runtime-only credential validation.
- Modify `app.py`: request-time Firebase initialization.
- Modify `auth_service/auth_service.py`: request-time Firebase and legacy-only MSAL pair.
- Test `tests/test_scheduler_user_listing.py`: retain its existing direct `_fs` replacement regression.
- Test `tests/test_surface_c_dashboard_auth.py`: retain dashboard auth response regressions.
- Modify `tests/test_surface_c_device_flow.py`: patch the legacy pair getter rather than an eager app.
- Modify `auth_service/test_auth_service_isolation.py`: keep per-user isolation and assert no legacy fallback for new flows.
- Test `tests/test_process_user_service.py`: retain the existing process-user and exact health-body contracts.
- Modify `tests/test_test_collection_contract.py`: no-conftest, zero-constructor complete-inventory gate.
- Modify `tests/test_compound_nonviable_processing.py`: remove the module-scope Firestore constructor patch.
- Modify `tests/test_rubric_core_launch_draft_terminal_state.py`: remove the module-scope Firestore constructor assignment and import-only fake.
- Modify `tests/test_rubric_core_launch_draft_duplicate_retry.py`: remove the module-scope Firestore constructor assignment and import-only fake.
- Modify `tests/test_full_campaign_e2e.py`: remove the additional module-scope Firestore constructor assignment exposed by the temporal guard.
- Delete `conftest.py`: remove the test-only provider substitution that masks production imports.

### Task 0: Freeze the partial baseline and reproduce the real RED

**Files:**
- Inspect: `conftest.py`
- Modify: `tests/test_test_collection_contract.py`
- Create: `tests/test_runtime_provider_initialization.py`
- Inspect: the five production sites named in the design

- [ ] **Step 1: Verify the isolated worktree and immutable ancestry**

Run:

```bash
git status --short --branch
test "$(git rev-parse HEAD)" = c1ba4714381d26c6eef5c8f1a2a2a8b8bff67a30
git merge-base --is-ancestor \
  6caa8ec14cc525299cfb8ed13bdd219f35c4322b \
  c1ba4714381d26c6eef5c8f1a2a2a8b8bff67a30
```

Expected: clean new implementation branch at exact partial head and ancestor
check exit 0. Do not use the dirty canonical checkout or rebase onto `main`.

If this isolated worktree has no virtual environment, create the ignored local
environment before any test command:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
```

Expected: `.venv/bin/python` exists and dependency installation exits 0. Never
copy credentials or a service-account file into the worktree.

- [ ] **Step 2: Prove the masking harness is currently GREEN**

Run:

```bash
env -u OPENAI_API_KEY \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u AZURE_API_APP_ID \
    -u AZURE_API_CLIENT_SECRET \
    -u FIREBASE_API_KEY \
    PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_test_collection_contract.py
```

Expected at the inherited partial head: `5 passed, 13 subtests passed`. This is
the control showing that `conftest.py` currently makes the contract pass.

- [ ] **Step 3: Change the subprocess probe to bypass all conftests and verify RED**

In `test_whole_repo_collection_completes_with_credential_absent_inventory`, add
`--noconftest` before `--collect-only` and remove the test that approves
constructor replacement. Remove the now-unused
`from types import SimpleNamespace` import in the same edit. Do not delete
`conftest.py` yet.

In `_credential_absent_env()`, add:

```python
env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
```

This keeps the guarded child scoped to pytest core and repository code rather
than arbitrary workstation plugins; the complete node-ID assertion still
proves repository inventory.

The subprocess command list must contain:

```python
[
    sys.executable,
    "-m",
    "pytest",
    "--noconftest",
    "--collect-only",
    "-q",
    "-p",
    "no:cacheprovider",
]
```

Replace the existing `sitecustomize.py` body with this exact guard. Protected
module attributes reject replacement immediately and are checked again at
process exit; class-method identities are checked at exit:

```python
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
        log_file.write(name + "\n")


def blocked(name):
    def fail(*args, **kwargs):
        record("BOUNDARY_CALLED:" + name)
        raise RuntimeError("COLLECTION_EFFECT_ATTEMPTED:" + name)
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
    }:
        record("BOUNDARY_CALLED:audit:" + event)
        raise RuntimeError("COLLECTION_EFFECT_ATTEMPTED:audit:" + event)


sys.addaudithook(reject_socket_audit)

# SDK import dependencies perform a caught IPv6-capability socket construction
# inside urllib3. Preload them before the collection measurement boundary while
# the audit hook already forbids DNS and connection attempts.
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
            raise RuntimeError("COLLECTION_GUARD_REPLACED:" + guard["name"])
        super().__setattr__(attribute, value)

    def __delattr__(self, attribute):
        guard = protected_module_attributes.get((id(self), attribute))
        if guard is not None:
            record("BOUNDARY_REPLACED:" + guard["name"])
            raise RuntimeError("COLLECTION_GUARD_REPLACED:" + guard["name"])
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
protect_attribute(original_socket_type, "connect", "socket.socket.connect")


class BlockedSocket(original_socket_type):
    def __new__(cls, *args, **kwargs):
        record("BOUNDARY_CALLED:socket.socket")
        raise RuntimeError("COLLECTION_EFFECT_ATTEMPTED:socket.socket")


protect_module_attribute(socket, "socket", "socket.socket", BlockedSocket)
protect_module_attribute(
    socket, "create_connection", "socket.create_connection"
)
protect_module_attribute(urllib.request, "urlopen", "urllib.request.urlopen")
protect_attribute(
    http.client.HTTPConnection, "request", "http.client.HTTPConnection.request"
)
protect_attribute(
    http.client.HTTPConnection, "connect", "http.client.HTTPConnection.connect"
)
protect_attribute(
    http.client.HTTPSConnection, "request", "http.client.HTTPSConnection.request"
)
protect_attribute(
    http.client.HTTPSConnection, "connect", "http.client.HTTPSConnection.connect"
)

protect_module_attribute(firestore, "Client", "firestore.Client")
protect_module_attribute(
    firebase_admin, "initialize_app", "firebase_admin.initialize_app"
)
protect_module_attribute(msal, "PublicClientApplication", "msal.PublicClientApplication")
protect_module_attribute(openai, "OpenAI", "openai.OpenAI")
protect_module_attribute(requests.api, "request", "requests.api.request")
protect_attribute(
    requests.sessions.Session, "request", "requests.sessions.Session.request"
)

if httpx is not None:
    protect_attribute(httpx.Client, "send", "httpx.Client.send")
    protect_attribute(httpx.AsyncClient, "send", "httpx.AsyncClient.send")


@atexit.register
def assert_guard_identity():
    for guard in module_guards:
        current = getattr(guard["module"], guard["attribute"], None)
        if current is not guard["value"]:
            record("BOUNDARY_IDENTITY_LOST:" + guard["name"])
    for owner, attribute, name, expected in attribute_guards:
        if getattr(owner, attribute, None) is not expected:
            record("BOUNDARY_IDENTITY_LOST:" + name)


Path(os.environ["COLLECTION_GUARD_READY"]).write_text(
    "ready", encoding="utf-8"
)
```

Keep the existing parent assertion `boundary_calls == []`. Because the guard
logs calls, replacement attempts, and exit-time identity loss, that one
assertion proves the blocker identities survived the entire child lifetime—not
merely that they were restored before the parent read the log.

- [ ] **Step 4: Add the health import RED in its own child interpreter**

Create `tests/test_runtime_provider_initialization.py` with this health-only
fresh-child RED. This new parent test module has no production import:

```python
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
```

Add this class after the helper:

```python
class RuntimeProviderInitializationTests(unittest.TestCase):
    def test_health_import_constructs_no_provider_socket_or_http_client(self):
        self.assertEqual("HEALTH_OFFLINE_OK", _run_credential_free_health_probe())
```

The child deliberately does not call `/process-user`.

- [ ] **Step 5: Run both contracts and capture the expected RED**

```bash
env -u OPENAI_API_KEY \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u AZURE_API_APP_ID \
    -u AZURE_API_CLIENT_SECRET \
    -u FIREBASE_API_KEY \
    PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_test_collection_contract.py \
    tests/test_runtime_provider_initialization.py
```

Expected: FAIL for two intended reasons. Whole collection emits at least one
`BOUNDARY_CALLED:<constructor>` entry, or reaches one of the four obsolete
module-scope substitutions first and emits
`BOUNDARY_REPLACED:firestore.Client`. The health child fails with
`offline boundary called: firestore.Client` or `offline boundary called:
openai.OpenAI`. The inherited `conftest.py` cannot make either child pass. A
socket or HTTP entry is an additional real defect, not an acceptable substitute
RED for an eager constructor.

- [ ] **Step 6: Commit only the stronger failing contracts**

```bash
git add tests/test_test_collection_contract.py \
  tests/test_runtime_provider_initialization.py
git commit -m "test: expose eager provider construction during collection"
```

### Task 1: Build the lazy proxy with concurrency and retry guarantees

**Files:**
- Create: `email_automation/lazy_provider.py`
- Create: `tests/test_lazy_provider.py`

- [ ] **Step 1: Write the proxy RED**

Create `tests/test_lazy_provider.py` with these exact imports and test methods:

```python
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest

from email_automation.lazy_provider import LazyProviderProxy


class LazyProviderProxyTests(unittest.TestCase):
    def test_concurrent_first_access_constructs_exactly_once(self):
        calls = 0
        calls_lock = threading.Lock()
        instance = object()

        def factory():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.03)
            return instance

        proxy = LazyProviderProxy("test", factory)
        workers = 16
        barrier = threading.Barrier(workers + 1)

        def read():
            barrier.wait(timeout=5)
            return proxy.get()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(read) for _ in range(workers)]
            barrier.wait(timeout=5)
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(1, calls)
        self.assertTrue(all(value is instance for value in results))

    def test_factory_failure_is_not_cached(self):
        attempts = 0
        instance = object()

        def factory():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first failure")
            return instance

        proxy = LazyProviderProxy("retry", factory)
        with self.assertRaisesRegex(RuntimeError, "first failure"):
            proxy.get()
        self.assertIs(instance, proxy.get())
        self.assertEqual(2, attempts)

    def test_repr_and_initialized_do_not_construct(self):
        calls = []
        proxy = LazyProviderProxy("quiet", lambda: calls.append(1) or object())
        self.assertFalse(proxy.initialized)
        self.assertIn("quiet", repr(proxy))
        self.assertEqual([], calls)

    def test_attribute_access_delegates_and_constructs_once(self):
        calls = []

        class Provider:
            def collection(self, name):
                return ("collection", name)

        proxy = LazyProviderProxy(
            "delegate", lambda: calls.append("factory") or Provider()
        )
        self.assertTrue(proxy)
        self.assertEqual([], calls)
        self.assertEqual(("collection", "users"), proxy.collection("users"))
        self.assertEqual(["factory"], calls)
```

- [ ] **Step 2: Run the new tests and verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_lazy_provider.py
```

Expected: import error for `email_automation.lazy_provider`.

- [ ] **Step 3: Implement the minimal proxy**

Create `email_automation/lazy_provider.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Generic, TypeVar, cast

T = TypeVar("T")
_UNSET = object()


class LazyProviderProxy(Generic[T]):
    def __init__(self, name: str, factory: Callable[[], T]) -> None:
        self._name = name
        self._factory = factory
        self._instance: T | object = _UNSET
        self._lock = Lock()

    @property
    def initialized(self) -> bool:
        return self._instance is not _UNSET

    def get(self) -> T:
        instance = self._instance
        if instance is _UNSET:
            with self._lock:
                instance = self._instance
                if instance is _UNSET:
                    instance = self._factory()
                    self._instance = instance
        return cast(T, instance)

    def __getattr__(self, attribute: str):
        return getattr(self.get(), attribute)

    def __repr__(self) -> str:
        state = "ready" if self.initialized else "uninitialized"
        return f"LazyProviderProxy(name={self._name!r}, state={state})"
```

- [ ] **Step 4: Run the focused test GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_lazy_provider.py
```

Expected: all proxy tests pass; no provider package or credential is needed.

- [ ] **Step 5: Commit the isolated utility**

```bash
git add email_automation/lazy_provider.py tests/test_lazy_provider.py
git commit -m "feat: add thread-safe lazy provider proxy"
```

### Task 2: Migrate Firestore and OpenAI globals without breaking patch seams

**Files:**
- Modify: `email_automation/clients.py`
- Modify: `scheduler_runner.py`
- Modify: `tests/test_runtime_provider_initialization.py`
- Test: `tests/test_scheduler_user_listing.py`

- [ ] **Step 1: Extend the fresh-child probe harness**

Task 0 already created `tests/test_runtime_provider_initialization.py` with no
production-module imports at module scope. Add this constant and two general
helpers after `_run_credential_free_health_probe()`:

```python
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
```

Every production provider import, first-use, retry, and barrier test added from
this point calls `_run_probe`. Do not use `importlib.reload`, do not assign a
production module to a local name after it has already been referenced, and do
not share `sys.modules` state between probes.

- [ ] **Step 2: Add exact import and failure-then-retry RED probes**

Add these methods to the existing `RuntimeProviderInitializationTests` class:

```python
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
```

Each import and each retry proof above has its own child interpreter.

- [ ] **Step 3: Add exact concurrent first-use probes**

Add one more test whose child uses a start barrier for each proxy:

```python
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
```

The `workers + 1` barrier is the start gun; it is intentionally outside the
factory because only one thread is permitted to enter the factory lock.

- [ ] **Step 4: Run the focused tests and verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_provider_initialization.py
```

Expected: the clients import child fails on `firestore.Client`; the
credential-empty scheduler import child fails with `Missing required env vars`;
and the retry/concurrency children fail when the current eager import consumes
the constructor side effect before the test's first-use step. No failure may be
an `UnboundLocalError`, parent-process credential error, or
stale-module/duplicate-route error.

- [ ] **Step 5: Replace the eager objects in `clients.py`**

Use:

```python
from .lazy_provider import LazyProviderProxy

_fs = LazyProviderProxy(
    "email_automation.clients.firestore",
    lambda: firestore.Client(),
)
client = LazyProviderProxy(
    "email_automation.clients.openai",
    lambda: openai.OpenAI(api_key=OPENAI_API_KEY),
)
```

Delete the `openai.api_key` assignment. Do not rename `_fs`, `client`, or any
existing helper.

- [ ] **Step 6: Defer scheduler validation and constructors**

Replace the import-time raises with:

```python
def _require_runtime_config() -> None:
    if not CLIENT_ID or not CLIENT_SECRET or not FIREBASE_API_KEY:
        raise RuntimeError("Missing required env vars")
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY env var")


def _new_firestore_client():
    _require_runtime_config()
    return firestore.Client()


def _new_openai_client():
    _require_runtime_config()
    return openai.OpenAI(api_key=OPENAI_API_KEY)


client = LazyProviderProxy("scheduler_runner.openai", _new_openai_client)
_fs = LazyProviderProxy("scheduler_runner.firestore", _new_firestore_client)
```

Call `_require_runtime_config()` as the first statement of `list_user_ids()`,
as the first statement of `refresh_and_process_user()`, and as the first
statement inside the `__main__` block. This ensures the storage-listing HTTP
request cannot precede validation. Remove the import-time `openai.api_key`
mutation. Do not touch the legacy send disablement.

- [ ] **Step 7: Run focused GREEN and patch-seam regressions**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_provider_initialization.py \
  tests/test_scheduler_user_listing.py
```

Expected: every fresh-child import/retry/concurrency probe passes; both
constructors are called twice in each failure/retry child and once in the
barrier child; and the missing-config child fails before its HTTP blocker.
`tests/test_scheduler_user_listing.py` remains green, proving direct
replacement of the whole `_fs` global still intercepts listing.

- [ ] **Step 8: Commit the two production sites**

```bash
git add email_automation/clients.py scheduler_runner.py \
  tests/test_runtime_provider_initialization.py
git commit -m "fix: defer backend provider clients until runtime use"
```

### Task 3: Move Firebase Admin initialization to authenticated request time

**Files:**
- Modify: `app.py`
- Modify: `auth_service/auth_service.py`
- Test: `tests/test_surface_c_dashboard_auth.py`
- Test: `tests/test_surface_c_device_flow.py`
- Modify: `tests/test_runtime_provider_initialization.py`

- [ ] **Step 1: Write failing Firebase timing tests**

Append these two fresh-child tests to
`RuntimeProviderInitializationTests`. They prove that import and a request with
no Bearer token never initialize Firebase:

```python
    def test_app_import_and_missing_bearer_do_not_initialize_firebase(self):
        self.assertEqual("NO_INIT", _run_probe("""
            from unittest.mock import patch
            import firebase_admin
            calls = []

            def initialize():
                calls.append("initialize_app")
                return object()

            with patch.object(
                firebase_admin,
                "initialize_app",
                side_effect=initialize,
            ):
                import app
                response = app.app.test_client().post("/api/trigger-scheduler", json={})
                assert response.status_code == 401
                assert response.get_json()["error"] == "Authentication required"
                assert calls == []
            print("NO_INIT")
        """, e2e=True))

    def test_auth_service_import_and_missing_bearer_do_not_initialize_firebase(self):
        self.assertEqual("NO_INIT", _run_probe("""
            import sys
            from pathlib import Path
            from unittest.mock import patch
            import firebase_admin
            sys.path.insert(0, str(Path.cwd() / "auth_service"))
            calls = []

            def initialize():
                calls.append("initialize_app")
                return object()

            with patch.object(
                firebase_admin,
                "initialize_app",
                side_effect=initialize,
            ):
                import auth_service
                response = auth_service.app.test_client().post(
                    "/start-device-flow", json={}
                )
                assert response.status_code == 401
                assert response.get_json()["error"] == "Authentication required"
                assert calls == []
            print("NO_INIT")
        """, e2e=True))
```

- [ ] **Step 2: Add exact Firebase failure/retry route probes**

Append these complete tests. Each child gets one failed initialization and then
one successful retry; neither child reuses a production module imported by the
parent:

```python
    def test_app_firebase_initialization_failure_is_fail_closed_and_retryable(self):
        self.assertEqual("RETRY_OK", _run_probe("""
            from unittest.mock import patch
            import firebase_admin
            state = {"ready": False}

            def get_app():
                if not state["ready"]:
                    raise ValueError("no default app")
                return object()

            def initialize():
                if initialize.calls == 0:
                    initialize.calls += 1
                    raise RuntimeError("adc unavailable")
                initialize.calls += 1
                state["ready"] = True
                return object()
            initialize.calls = 0

            with patch.object(firebase_admin, "get_app", side_effect=get_app), \
                 patch.object(firebase_admin, "initialize_app", side_effect=initialize), \
                 patch("firebase_admin.auth.verify_id_token", return_value={"uid": "u1"}):
                import app
                client = app.app.test_client()
                headers = {"Authorization": "Bearer test-token"}
                first = client.post("/api/trigger-scheduler", json={}, headers=headers)
                second = client.post("/api/trigger-scheduler", json={}, headers=headers)
                assert first.status_code == 401
                assert first.get_json()["error"] == "Authentication unavailable"
                assert second.status_code == 503
                assert initialize.calls == 2
            print("RETRY_OK")
        """, e2e=True))

    def test_auth_service_firebase_failure_is_fail_closed_and_retryable(self):
        self.assertEqual("RETRY_OK", _run_probe("""
            import sys
            from pathlib import Path
            from unittest.mock import MagicMock, patch
            import firebase_admin
            sys.path.insert(0, str(Path.cwd() / "auth_service"))
            state = {"ready": False, "calls": 0}

            def get_app():
                if not state["ready"]:
                    raise ValueError("no default app")
                return object()

            def initialize():
                state["calls"] += 1
                if state["calls"] == 1:
                    raise RuntimeError("adc unavailable")
                state["ready"] = True
                return object()

            fake_app = MagicMock()
            fake_app.initiate_device_flow.return_value = {
                "message": "enter code", "user_code": "CODE"
            }
            fake_cache = MagicMock()
            with patch.object(firebase_admin, "get_app", side_effect=get_app), \
                 patch.object(firebase_admin, "initialize_app", side_effect=initialize), \
                 patch("firebase_admin.auth.verify_id_token", return_value={"uid": "u1"}):
                import auth_service
                with patch.object(
                    auth_service,
                    "_new_isolated_app",
                    return_value=(fake_app, fake_cache),
                ):
                    client = auth_service.app.test_client()
                    headers = {"Authorization": "Bearer test-token"}
                    first = client.post("/start-device-flow", json={}, headers=headers)
                    second = client.post("/start-device-flow", json={}, headers=headers)
                assert first.status_code == 401
                assert first.get_json()["error"] == "Authentication unavailable"
                assert second.status_code == 200
                assert state["calls"] == 2
            print("RETRY_OK")
        """, e2e=True))
```

- [ ] **Step 3: Add exact barrier-started Firebase request probes**

Add one test per Flask module. Both children assert that import performs zero
initializations, release 16 authenticated requests at one barrier, and count
one initialization plus 16 token verifications. Add this app child:

```python
    def test_app_firebase_initializes_once_under_concurrent_first_access(self):
        self.assertEqual("BARRIER_OK", _run_probe("""
            import threading
            import time
            from concurrent.futures import ThreadPoolExecutor
            from unittest.mock import patch
            import firebase_admin
            workers = 16
            state = {"ready": False, "init_calls": 0, "verify_calls": 0}
            state_lock = threading.Lock()

            def get_app():
                with state_lock:
                    if not state["ready"]:
                        raise ValueError("no default app")
                return object()

            def initialize():
                with state_lock:
                    state["init_calls"] += 1
                time.sleep(0.03)
                with state_lock:
                    state["ready"] = True
                return object()

            def verify(token, check_revoked=False):
                with state_lock:
                    state["verify_calls"] += 1
                return {"uid": token}

            with patch.object(firebase_admin, "get_app", side_effect=get_app), \
                 patch.object(firebase_admin, "initialize_app", side_effect=initialize), \
                 patch("firebase_admin.auth.verify_id_token", side_effect=verify):
                import app
                assert state == {
                    "ready": False, "init_calls": 0, "verify_calls": 0
                }

                @app.app.route("/__firebase_barrier_probe", methods=["POST"])
                @app.verify_firebase_token
                def firebase_barrier_probe():
                    return "", 204

                barrier = threading.Barrier(workers + 1)
                def worker(index):
                    barrier.wait(timeout=5)
                    with app.app.test_client() as client:
                        return client.post(
                            "/__firebase_barrier_probe",
                            json={},
                            headers={"Authorization": f"Bearer user-{index}"},
                        ).status_code
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(worker, index) for index in range(workers)]
                    barrier.wait(timeout=5)
                    statuses = [future.result(timeout=5) for future in futures]
                assert statuses == [204] * workers
                assert state == {
                    "ready": True, "init_calls": 1, "verify_calls": workers
                }
            print("BARRIER_OK")
        """, e2e=True))
```

Add the auth-service barrier child in full:

```python
    def test_auth_service_firebase_initializes_once_under_concurrent_first_access(self):
        self.assertEqual("BARRIER_OK", _run_probe("""
            import sys
            import threading
            import time
            from concurrent.futures import ThreadPoolExecutor
            from pathlib import Path
            from unittest.mock import patch
            import firebase_admin
            sys.path.insert(0, str(Path.cwd() / "auth_service"))
            workers = 16
            state = {"ready": False, "init_calls": 0, "verify_calls": 0}
            state_lock = threading.Lock()

            def get_app():
                with state_lock:
                    if not state["ready"]:
                        raise ValueError("no default app")
                return object()

            def initialize():
                with state_lock:
                    state["init_calls"] += 1
                time.sleep(0.03)
                with state_lock:
                    state["ready"] = True
                return object()

            def verify(token):
                with state_lock:
                    state["verify_calls"] += 1
                return {"uid": token}

            with patch.object(firebase_admin, "get_app", side_effect=get_app), \
                 patch.object(firebase_admin, "initialize_app", side_effect=initialize), \
                 patch("firebase_admin.auth.verify_id_token", side_effect=verify):
                import auth_service
                assert state == {
                    "ready": False, "init_calls": 0, "verify_calls": 0
                }

                @auth_service.app.route(
                    "/__firebase_barrier_probe", methods=["POST"]
                )
                @auth_service.verify_firebase_token
                def firebase_barrier_probe():
                    return "", 204

                barrier = threading.Barrier(workers + 1)
                def worker(index):
                    barrier.wait(timeout=5)
                    with auth_service.app.test_client() as client:
                        return client.post(
                            "/__firebase_barrier_probe",
                            json={},
                            headers={"Authorization": f"Bearer user-{index}"},
                        ).status_code
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(worker, index) for index in range(workers)]
                    barrier.wait(timeout=5)
                    statuses = [future.result(timeout=5) for future in futures]
                assert statuses == [204] * workers
                assert state == {
                    "ready": True, "init_calls": 1, "verify_calls": workers
                }
            print("BARRIER_OK")
        """, e2e=True))
```

No shared helper may import either production module in the parent process.

- [ ] **Step 4: Run the Firebase-focused tests and verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_provider_initialization.py
```

Expected: both `NO_INIT` children fail at `assert calls == []` because current
imports call `initialize_app`; retry probes fail because current decorators do
not call a retryable getter; barrier probes fail their zero-call import
assertion or because `_get_firebase_auth` does not exist. These are the only
acceptable RED reasons; no production application is imported in the parent
pytest process.

- [ ] **Step 5: Implement the local getters**

In both Flask modules, replace the entire defensive Firebase import block with:

```python
try:
    import firebase_admin
    from firebase_admin import auth as firebase_auth
except Exception as _fb_import_err:  # pragma: no cover - env dependent
    firebase_admin = None
    firebase_auth = None
    print(
        f"⚠️ firebase_admin unavailable: {type(_fb_import_err).__name__}",
        flush=True,
    )
```

This removes every import-time `firebase_admin._apps` read and
`initialize_app()` call. Add this code once in each module after that block:

```python
_firebase_init_lock = threading.Lock()


def _get_firebase_auth():
    if firebase_admin is None or firebase_auth is None:
        raise RuntimeError("firebase_admin unavailable")
    with _firebase_init_lock:
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app()
    return firebase_auth
```

In `app.py`, replace the current `if firebase_auth is None` block and token
verification `try` with:

```python
try:
    auth_client = _get_firebase_auth()
except Exception as exc:
    print(
        f"❌ Firebase auth unavailable: {type(exc).__name__}",
        flush=True,
    )
    return jsonify({"success": False, "error": "Authentication unavailable"}), 401
try:
    decoded = auth_client.verify_id_token(token, check_revoked=check_revoked)
except Exception as exc:
    print(
        f"⚠️ Firebase token verification failed: {type(exc).__name__}",
        flush=True,
    )
    return jsonify({"success": False, "error": "Invalid authentication token"}), 401
```

Keep this replacement after the existing nonempty Bearer-token checks. It is
followed by this exact existing uid block:

```python
uid = decoded.get("uid") if isinstance(decoded, dict) else None
if not _is_nonempty_str(uid):
    return jsonify({"success": False, "error": "Invalid authentication token"}), 401
g.firebase_uid = uid
return func(*args, **kwargs)
```

In `auth_service/auth_service.py`, replace its current
`if firebase_auth is None` block and token verification `try` with:

```python
try:
    auth_client = _get_firebase_auth()
except Exception as exc:
    print(
        f"❌ Firebase auth unavailable: {type(exc).__name__}",
        flush=True,
    )
    return jsonify({"status": "failed", "error": "Authentication unavailable"}), 401
try:
    decoded = auth_client.verify_id_token(token)
except Exception as exc:
    print(
        f"⚠️ Firebase token verification failed: {type(exc).__name__}",
        flush=True,
    )
    return jsonify({"status": "failed", "error": "Invalid authentication token"}), 401
```

Immediately after the auth-service verification block, retain this exact code:

```python
uid = decoded.get("uid") if isinstance(decoded, dict) else None
if not _is_nonempty_str(uid):
    return jsonify({"status": "failed", "error": "Invalid authentication token"}), 401
g.firebase_uid = uid
return f(*args, **kwargs)
```

- [ ] **Step 6: Run Firebase GREEN**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_provider_initialization.py \
  tests/test_surface_c_dashboard_auth.py \
  tests/test_surface_c_device_flow.py
```

Expected: all six fresh-child Firebase tests pass. Both barrier children report
exactly one initializer and 16 verifier calls after 16 simultaneous protected
requests, and both retry children recover on their second request without
caching the initialization failure.

- [ ] **Step 7: Commit the Firebase boundary**

```bash
git add app.py auth_service/auth_service.py \
  tests/test_runtime_provider_initialization.py
git commit -m "fix: initialize Firebase only at authenticated boundaries"
```

### Task 4: Make the auth-service legacy MSAL fallback lazy and isolated

**Files:**
- Modify: `auth_service/auth_service.py`
- Modify: `auth_service/test_auth_service_isolation.py`
- Modify: `tests/test_surface_c_device_flow.py`
- Modify: `tests/test_runtime_provider_initialization.py`

- [ ] **Step 1: Repair and commit the stale inherited test harness before RED**

In `auth_service/test_auth_service_isolation.py`, add these exact fake Firebase
objects next to the fake MSAL objects:

```python
def auth_headers(uid):
    return {"Authorization": f"Bearer {uid}"}


fake_firebase_auth = types.ModuleType("firebase_admin.auth")


def verify_id_token(token):
    return {"uid": token}


fake_firebase_auth.verify_id_token = verify_id_token

fake_firebase_admin = types.ModuleType("firebase_admin")
fake_firebase_admin._apps = {"default": object()}
fake_firebase_admin.get_app = lambda: fake_firebase_admin._apps["default"]
fake_firebase_admin.initialize_app = lambda: fake_firebase_admin._apps["default"]
fake_firebase_admin.auth = fake_firebase_auth
```

Replace the existing `patch.dict(sys.modules, ...)` mapping with these four
entries:

```python
{
    "msal": fake_msal,
    "firebase_helpers": fake_fh,
    "firebase_admin": fake_firebase_admin,
    "firebase_admin.auth": fake_firebase_auth,
}
```

Add these methods to the test class:

```python
def _start(self, uid):
    return self.client.post(
        "/start-device-flow", json={}, headers=auth_headers(uid)
    )

def _complete(self, uid):
    return self.client.post(
        "/complete-device-flow", json={}, headers=auth_headers(uid)
    )
```

Make these literal call transformations everywhere in that file:

```python
self.client.post("/start-device-flow", json={"uid": "userA"})
# becomes
self._start("userA")

self.client.post("/start-device-flow", json={"uid": "userB"})
# becomes
self._start("userB")

self.client.post("/complete-device-flow", json={"uid": "userA"})
# becomes
self._complete("userA")

self.client.post("/complete-device-flow", json={"uid": "userB"})
# becomes
self._complete("userB")

self.client.post("/complete-device-flow", json={"uid": "ghost"})
# becomes
self._complete("ghost")
```

Then run:

```bash
rg -n 'self\.client\.post\("/(start|complete)-device-flow' \
  auth_service/test_auth_service_isolation.py
```

Expected: exit 1 with no matches; every authenticated route call now supplies
the exact Bearer uid through `_start` or `_complete`.

Change the expiration mutation to:

```python
self.mod.flows["userA"]["ts"] -= self.mod._FLOW_TTL_SECONDS + 1
```

and change both stale `no_pending_flow` expectations to the current exact text:

```python
self.assertEqual(resp.get_json()["error"], "No active device flow")
```

Run and commit this harness-only correction before introducing new expectations:

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  auth_service/test_auth_service_isolation.py
git add auth_service/test_auth_service_isolation.py
git commit -m "test: align auth isolation harness with current auth contract"
```

Expected: the inherited seven failures become green without changing production
code. If any fail for a different production-contract mismatch, stop and amend
the design instead of weakening an assertion.

- [ ] **Step 2: Add exact MSAL import, new-flow, partial-entry, barrier, and retry RED**

Add this test to the repaired auth isolation suite:

```python
def test_new_flows_never_use_legacy_msal_fallback(self):
    self.mod._created_apps.clear()
    with patch.object(self.mod, "_get_legacy_msal_pair") as legacy:
        first = self._start("userA")
        second = self._start("userB")
    self.assertEqual(first.status_code, 200)
    self.assertEqual(second.status_code, 200)
    self.assertEqual(len(self.mod._created_apps), 2)
    self.assertIsNot(
        self.mod.flows["userA"]["app"], self.mod.flows["userB"]["app"]
    )
    legacy.assert_not_called()
```

In `tests/test_surface_c_device_flow.py`, replace the old `authmod.msal_app`
patches in `DeviceFlowBase.setUp` with one complete legacy pair while retaining
the existing `init_mock`/`acq_mock` assertion names:

```python
self.legacy_cache = MagicMock(name="legacy_cache")
self.legacy_cache.serialize.return_value = "{}"
self.legacy_app = MagicMock(name="legacy_app")
self.legacy_app.initiate_device_flow.return_value = dict(FAKE_FLOW)
self.legacy_app.acquire_token_by_device_flow.return_value = {
    "access_token": "AT", "token_type": "Bearer"
}
self.init_mock = self.legacy_app.initiate_device_flow
self.acq_mock = self.legacy_app.acquire_token_by_device_flow
self._p_legacy = patch.object(
    authmod,
    "_get_legacy_msal_pair",
    return_value=(self.legacy_app, self.legacy_cache),
)
self._p_legacy.start()
```

Keep the existing isolated fake setup, including these assignments:

```python
self.fake_app.initiate_device_flow = self.init_mock
self.fake_app.acquire_token_by_device_flow = self.acq_mock
```

Replace `_p_init.stop()` and `_p_acq.stop()` in `tearDown` with
`self._p_legacy.stop()`. Seeded legacy entries continue to omit `app` and
`cache`, so their existing `self.acq_mock` assertions exercise the getter's
fallback app. Add this exact partial-entry test:

```python
def test_partial_isolated_entry_fails_closed_without_legacy_fallback(self):
    authmod.flows["web_user"] = {
        "flow": dict(FAKE_FLOW),
        "app": self.fake_app,
        "ts": time.time(),
    }
    self._p_legacy.stop()
    try:
        with patch.object(authmod, "_get_legacy_msal_pair") as legacy, \
             patch.object(authmod, "upload_token") as upload:
            response = self.client.post(
                "/complete-device-flow", json={}, headers=AUTH
            )
        self.assertEqual(response.status_code, 400)
        legacy.assert_not_called()
        upload.assert_not_called()
    finally:
        self._p_legacy.start()
```

Append the fresh-child import test to
`RuntimeProviderInitializationTests`:

```python
def test_auth_service_import_constructs_no_msal_app(self):
    self.assertEqual("NO_MSAL", _run_probe("""
        import sys
        from pathlib import Path
        from unittest.mock import patch
        import msal
        sys.path.insert(0, str(Path.cwd() / "auth_service"))
        with patch.object(
            msal,
            "PublicClientApplication",
            side_effect=AssertionError("MSAL app constructed during import"),
        ):
            import auth_service
        print("NO_MSAL")
    """, e2e=True))
```

Append this exact barrier probe. It asserts zero fallback construction before
the barrier and the same one app/cache pair after 16 concurrent first-use
getter calls:

```python
def test_legacy_msal_pair_constructs_once_under_concurrent_first_use(self):
    self.assertEqual("MSAL_BARRIER_OK", _run_probe("""
        import sys
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from unittest.mock import patch
        import msal
        sys.path.insert(0, str(Path.cwd() / "auth_service"))
        workers = 16
        counts = {"app": 0, "cache": 0}
        count_lock = threading.Lock()

        class FakeCache:
            def __init__(self):
                with count_lock:
                    counts["cache"] += 1
            def serialize(self):
                return "cache"

        class FakeApp:
            def acquire_token_by_device_flow(self, flow):
                return {"access_token": "token"}

        def make_app(client_id, authority=None, token_cache=None):
            with count_lock:
                counts["app"] += 1
            time.sleep(0.03)
            return FakeApp()

        with patch.object(msal, "PublicClientApplication", side_effect=make_app), \
             patch.object(msal, "SerializableTokenCache", side_effect=FakeCache):
            import auth_service
            assert counts == {"app": 0, "cache": 0}
            barrier = threading.Barrier(workers + 1)
            def worker():
                barrier.wait(timeout=5)
                return auth_service._get_legacy_msal_pair()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(worker) for _ in range(workers)]
                barrier.wait(timeout=5)
                pairs = [future.result(timeout=5) for future in futures]
            assert all(pair is pairs[0] for pair in pairs)
            assert counts == {"app": 1, "cache": 1}
        print("MSAL_BARRIER_OK")
    """, e2e=True))
```

Append this exact failure/retry probe:

```python
def test_legacy_msal_constructor_failure_is_not_cached(self):
    self.assertEqual("MSAL_RETRY_OK", _run_probe("""
        import sys
        import time
        from pathlib import Path
        from unittest.mock import patch
        import firebase_admin
        import msal
        sys.path.insert(0, str(Path.cwd() / "auth_service"))
        counts = {"app": 0, "cache": 0}

        class FakeCache:
            def __init__(self):
                counts["cache"] += 1
            def serialize(self):
                return "cache"

        class FakeApp:
            def acquire_token_by_device_flow(self, flow):
                return {"access_token": "token"}

        def make_app(client_id, authority=None, token_cache=None):
            counts["app"] += 1
            if counts["app"] == 1:
                raise RuntimeError("first MSAL failure")
            return FakeApp()

        with patch.object(firebase_admin, "get_app", return_value=object()), \
             patch("firebase_admin.auth.verify_id_token", return_value={"uid": "legacy"}), \
             patch.object(msal, "PublicClientApplication", side_effect=make_app), \
             patch.object(msal, "SerializableTokenCache", side_effect=FakeCache):
            import auth_service
            auth_service.flows["legacy"] = {
                "flow": {"code": "legacy"}, "ts": time.time()
            }
            with patch.object(auth_service, "upload_token"):
                with auth_service.app.test_client() as client:
                    headers = {"Authorization": "Bearer legacy"}
                    first = client.post("/complete-device-flow", json={}, headers=headers)
                    second = client.post("/complete-device-flow", json={}, headers=headers)
            assert first.status_code == 500
            assert first.get_json()["error"] == "Internal server error"
            assert second.status_code == 200
            assert counts == {"app": 2, "cache": 2}
        print("MSAL_RETRY_OK")
    """, e2e=True))
```

- [ ] **Step 3: Run the new MSAL contracts and verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  auth_service/test_auth_service_isolation.py \
  tests/test_surface_c_device_flow.py \
  tests/test_runtime_provider_initialization.py
```

Expected: the already-repaired inherited harness stays green. The new tests fail
only because `_get_legacy_msal_pair` is absent or the current import constructs
the blocked eager app. Barrier/retry children must not fail on auth, network, or
test-fixture errors.

- [ ] **Step 4: Implement `_get_legacy_msal_pair()`**

Delete eager `cache` and `msal_app`. Insert this code where those globals were:

```python
_legacy_msal_lock = threading.Lock()
_legacy_msal_pair = None


def _get_legacy_msal_pair():
    global _legacy_msal_pair
    if _legacy_msal_pair is None:
        with _legacy_msal_lock:
            if _legacy_msal_pair is None:
                token_cache = SerializableTokenCache()
                legacy_app = PublicClientApplication(
                    CLIENT_ID,
                    authority=AUTHORITY,
                    token_cache=token_cache,
                )
                _legacy_msal_pair = (legacy_app, token_cache)
    return _legacy_msal_pair
```

Replace the existing `entry.get("app") or msal_app` /
`entry.get("cache") or cache` fallback in `complete_flow()` with:

```python
entry_app = entry.get("app")
entry_cache = entry.get("cache")
if (entry_app is None) != (entry_cache is None):
    return jsonify({"status": "failed", "error": _GENERIC_BAD_REQUEST}), 400

isolated = entry_app is not None
if isolated:
    app_, cache_ = entry_app, entry_cache
else:
    try:
        app_, cache_ = _get_legacy_msal_pair()
    except Exception as exc:
        print(f"MSAL fallback unavailable: {type(exc).__name__}", flush=True)
        return jsonify({"status": "failed", "error": _GENERIC_SERVER_ERROR}), 500
```

Do not edit the existing isolated-app constructor:

```python
def _new_isolated_app():
    """A fresh single-identity MSAL app + cache — never shared between users."""
    isolated_cache = SerializableTokenCache()
    isolated_app = PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, token_cache=isolated_cache
    )
    return isolated_app, isolated_cache
```

After `acquire_token_by_device_flow`, retain this isolated-account guard:

```python
if isolated:
    accounts = app_.get_accounts()
    if len(accounts) != 1:
        with _flows_lock:
            flows.pop(uid, None)
        return jsonify({
            "status": "failed",
            "error": (
                "identity_isolation_violation: expected 1 account, "
                f"got {len(accounts)}"
            ),
        }), 409
```

- [ ] **Step 5: Run MSAL GREEN and commit**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  auth_service/test_auth_service_isolation.py \
  tests/test_surface_c_device_flow.py \
  tests/test_runtime_provider_initialization.py
```

Expected: repaired inherited tests and every new fresh-child test pass; import
count is zero, new-flow legacy count is zero, barrier counts are exactly
one/one, retry counts are exactly two/two, and every new user still receives a
distinct pair.

```bash
git add auth_service/auth_service.py \
  auth_service/test_auth_service_isolation.py \
  tests/test_surface_c_device_flow.py \
  tests/test_runtime_provider_initialization.py
git commit -m "fix: defer legacy auth MSAL fallback construction"
```

### Task 5: Remove the masking hook and close the zero-constructor collection gate

**Files:**
- Delete: `conftest.py`
- Modify: `tests/test_test_collection_contract.py`
- Test: `tests/test_runtime_provider_initialization.py`
- Test: `tests/test_process_user_service.py`
- Modify: `tests/test_compound_nonviable_processing.py`
- Modify: `tests/test_rubric_core_launch_draft_terminal_state.py`
- Modify: `tests/test_rubric_core_launch_draft_duplicate_retry.py`
- Modify: `tests/test_full_campaign_e2e.py`

- [ ] **Step 1: Delete the provider-substitution hook**

Delete `conftest.py`; Task 0 already removed
`test_provider_client_boundary_is_collection_only_and_restored` and its
`SimpleNamespace` import. Keep the `--noconftest` subprocess option as a
defense against a future masking hook. Verify the obsolete approval did not
return:

```bash
rg -n 'test_provider_client_boundary_is_collection_only_and_restored|SimpleNamespace' \
  tests/test_test_collection_contract.py
```

Expected: exit 1 with no matches.

- [ ] **Step 2: Remove every obsolete module-scope Firestore substitution**

Make these exact edits; test-local `_fs` replacements used while a test is
executing remain valid and must not be removed.

In `tests/test_compound_nonviable_processing.py`, replace:

```python
with patch("google.cloud.firestore.Client", return_value=MagicMock()):
    from email_automation import ai_processing, campaign_safety, email as email_module, processing
```

with:

```python
from email_automation import ai_processing, campaign_safety, email as email_module, processing
```

Keep `MagicMock` and `patch` imported because the executable test bodies use
both names.

In `tests/test_rubric_core_launch_draft_terminal_state.py`, delete:

```python
import google.cloud.firestore as _gcf


class _FsForImport:
    """Stand-in returned by firestore.Client() so email_automation.clients is
    importable offline (no ADC). The real datastore boundary is faked per-call
    via mock.patch on email_automation.clients._fs; any accidental use here
    fails loudly instead of hitting real Firestore."""

    def __getattr__(self, name):
        raise AssertionError(
            f"real Firestore access '{name}' during test -- boundary not faked"
        )


# clients.py runs `_fs = firestore.Client()` at import time; stub it first.
_gcf.Client = lambda *a, **k: _FsForImport()
```

Leave the direct `from email_automation import email as email_mod` import and
the per-test `mock.patch("email_automation.clients._fs", fake_fs)` seams in
place.

In `tests/test_rubric_core_launch_draft_duplicate_retry.py`, delete:

```python
import google.cloud.firestore as _gcf


class _FakeFsForImport:
    """Stand-in returned by firestore.Client() so email_automation.clients is
    importable offline (no ADC). Never used for the actual claim logic; the
    per-call _FakeFs below supplies the transaction the unit exercises."""

    def transaction(self):
        return _FakeTransaction()

    def __getattr__(self, name):
        raise AssertionError(
            f"real Firestore access '{name}' during test -- boundary not faked"
        )


# Patch the Firestore client constructor (datastore boundary) BEFORE importing
# the production module, whose clients.py does `_fs = firestore.Client()` at
# import time.
_gcf.Client = lambda *a, **k: _FakeFsForImport()
```

Leave `_FakeTransaction`, `_FakeFs`, the transactional decorator patch, and
the direct `_claim_outbox_item` import in place; those fakes exercise runtime
logic and do not replace a constructor during collection.

The exhaustive guard also exposes one fourth import-only substitution not named
in the initial review. In `tests/test_full_campaign_e2e.py`, delete:

```python
import google.cloud.firestore as _gcf

# clients.py runs `_fs = firestore.Client()` at import time; stub it so the
# package imports offline.  The real datastore boundary is faked per-run below.
_gcf.Client = lambda *a, **k: mock.MagicMock()
```

Keep `mock`, the direct `email_automation` imports, `_FS_MODULES`, and the
per-run fake installation; the executable chained campaign still needs those
runtime fakes.

Run this static check:

```bash
rg -n 'with patch\("google\.cloud\.firestore\.Client"|_gcf\.Client\s*=' \
  tests/test_compound_nonviable_processing.py \
  tests/test_rubric_core_launch_draft_terminal_state.py \
  tests/test_rubric_core_launch_draft_duplicate_retry.py \
  tests/test_full_campaign_e2e.py
```

Expected: exit 1 with no matches. A remaining match means the temporal
constructor-identity proof can still be bypassed and blocks the next step.

- [ ] **Step 3: Confirm the pre-implementation health RED is still present**

Do not add or rewrite a health test here. Task 0 already committed
`test_health_import_constructs_no_provider_socket_or_http_client` before any
production change. Run this inventory check:

```bash
rg -n '^def _run_credential_free_health_probe|^    def test_health_import_constructs_no_provider_socket_or_http_client' \
  tests/test_runtime_provider_initialization.py
```

Expected: exactly two matches, one helper and one test. The helper still uses
`subprocess.run([sys.executable, "-c", source], ...)`; it has no production
import in the parent process and does not call `/process-user`.

- [ ] **Step 4: Verify the four focused suites and health probe**

```bash
E2E_TEST_MODE=true PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_compound_nonviable_processing.py \
  tests/test_rubric_core_launch_draft_terminal_state.py \
  tests/test_rubric_core_launch_draft_duplicate_retry.py \
  tests/test_full_campaign_e2e.py \
  tests/test_runtime_provider_initialization.py \
  tests/test_process_user_service.py
```

Expected: every test passes using only per-test fakes. The health child prints
only `HEALTH_OFFLINE_OK`; a provider constructor, socket construction/connect,
or HTTP request produces `offline boundary called: <name>` and fails the step.

- [ ] **Step 5: Close the whole-process identity and inventory contract**

The Task 0 sitecustomize guard records and rejects these boundaries:

```text
socket.socket
socket.socket.connect
socket.create_connection
audit:socket.__new__
audit:socket.connect
audit:socket.connect_ex
audit:socket.getaddrinfo
firestore.Client
firebase_admin.initialize_app
msal.PublicClientApplication
openai.OpenAI
requests.api.request
requests.sessions.Session.request
urllib.request.urlopen
http.client.HTTPConnection.request
http.client.HTTPConnection.connect
http.client.HTTPSConnection.request
http.client.HTTPSConnection.connect
httpx.Client.send (when installed)
httpx.AsyncClient.send (when installed)
```

```bash
env -u OPENAI_API_KEY \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u AZURE_API_APP_ID \
    -u AZURE_API_CLIENT_SECRET \
    -u FIREBASE_API_KEY \
    -u GMAIL_ADDRESS \
    -u GMAIL_APP_PASSWORD \
    PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_test_collection_contract.py \
    tests/test_runtime_provider_initialization.py \
    tests/test_process_user_service.py
```

Expected: all focused tests pass, subprocess collection exits 0 with a complete
nonzero inventory, and `collection_guard.log` is absent or empty. Because
`GuardedModule.__setattr__` rejects replacement immediately and the `atexit`
callback records identity loss, an empty log proves every protected
module-level blocker—including all four provider constructors—retained the same
object identity for the entire `--noconftest` child lifetime. It is not
sufficient for a test to restore a constructor before child exit. Class-method
HTTP blockers retain their exact exit identity, while the independent audit
hook makes any real socket construction, resolution, or connection fail even
if Python code temporarily replaces a method. Exit 0, no
`INTERNALERROR`/`SystemExit`, exact node-ID/count equality, and inclusion of the
auth-service and collection-contract node IDs remain mandatory. Do not
hard-code 2,640 because this plan intentionally adds tests.

- [ ] **Step 6: Commit the honest gate**

```bash
git add tests/test_test_collection_contract.py \
  tests/test_compound_nonviable_processing.py \
  tests/test_rubric_core_launch_draft_terminal_state.py \
  tests/test_rubric_core_launch_draft_duplicate_retry.py \
  tests/test_full_campaign_e2e.py
git rm conftest.py
git commit -m "test: require honest zero-constructor backend collection"
```

### Task 6: Run runtime regressions, full collection, and review handoff

**Files:**
- Verify all changed files
- Update documentation only if an exact command or count changed

- [ ] **Step 1: Run all focused runtime suites**

```bash
E2E_TEST_MODE=true PYTHONDONTWRITEBYTECODE=1 \
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_lazy_provider.py \
  tests/test_runtime_provider_initialization.py \
  tests/test_scheduler_user_listing.py \
  tests/test_process_user_service.py \
  tests/test_surface_c_dashboard_auth.py \
  tests/test_surface_c_device_flow.py \
  auth_service/test_auth_service_isolation.py \
  tests/test_scheduler_lease.py \
  tests/test_scheduler_scope.py
```

Expected: all pass with fakes only. No external provider, emulator, or network
is allowed.

- [ ] **Step 2: Run complete credential-free collection independently**

```bash
env -u OPENAI_API_KEY \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u AZURE_API_APP_ID \
    -u AZURE_API_CLIENT_SECRET \
    -u FIREBASE_API_KEY \
    -u GMAIL_ADDRESS \
    -u GMAIL_APP_PASSWORD \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m pytest --noconftest --collect-only -q \
    -p no:cacheprovider
```

Expected: exit 0 and a complete nonzero inventory. This independent command is
in addition to the self-testing subprocess guard.

- [ ] **Step 3: Run the hermetic safety matrix used by FDR-042**

```bash
env -u OPENAI_API_KEY \
    -u GOOGLE_APPLICATION_CREDENTIALS \
    -u AZURE_API_APP_ID \
    -u AZURE_API_CLIENT_SECRET \
    E2E_TEST_MODE=true \
    FIRESTORE_EMULATOR_HOST=127.0.0.1:9 \
    GOOGLE_CLOUD_PROJECT=sitesift-unit-test \
    PYTHONDONTWRITEBYTECODE=1 \
    .venv/bin/python -m pytest -q -p no:cacheprovider \
    tests/test_reply_reviews.py \
    tests/test_processing_reply_safety.py \
    tests/test_processing_completion_guards.py \
    tests/test_processing_retryability.py \
    tests/test_pending_responses.py \
    tests/test_operator_message_replay.py \
    tests/test_combo_stop_cancel_during_claim.py \
    tests/test_outbound_kill_switch.py \
    tests/test_outbox_safety.py \
    tests/test_outbox_reply_recipient_routing.py \
    tests/test_message_history_dedupe.py \
    tests/test_graph_send_health.py \
    tests/test_graph_retry_policy.py \
    tests/test_dead_letter_visibility.py \
    tests/test_dead_letter_recovery.py
```

Expected: the prior `387 passed, 250 subtests passed` baseline remains green or
increases only through intentionally added tests. Any changed existing outcome
blocks completion.

- [ ] **Step 4: Run static and diff checks**

```bash
.venv/bin/python -m py_compile \
  email_automation/lazy_provider.py \
  email_automation/clients.py \
  scheduler_runner.py \
  app.py \
  auth_service/auth_service.py
git diff --check c1ba4714381d26c6eef5c8f1a2a2a8b8bff67a30..HEAD
git status --short
```

Expected: compilation and diff check succeed; status is clean after commits.

- [ ] **Step 5: Request two-stage independent review**

First review the exact range against the design and #84 acceptance. Only after
spec approval, run a code-quality/security review emphasizing wrong-mailbox
isolation, thread safety, constructor retry, health purity, and direct patch
compatibility. A P0/P1/P2 or non-decreasing revision loop blocks push.

- [ ] **Step 6: Commit any evidence-only documentation and stop before push**

```bash
git status --short
git log --oneline --decorate c1ba4714381d26c6eef5c8f1a2a2a8b8bff67a30..HEAD
```

Expected: clean local branch. Report exact HEAD, test counts, inventory count,
review outcomes, and any pre-existing unrelated failures. Do not push, merge,
deploy, invoke `/process-user`, or run a provider/mailbox canary without a new
explicit authorization.
