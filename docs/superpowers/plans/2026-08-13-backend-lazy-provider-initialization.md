# Backend Lazy Provider Initialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make all repository test collection credential-free and zero-constructor by moving Firestore, Firebase Admin, OpenAI, and the auth service's legacy MSAL construction to their first real runtime use without changing live provider semantics.

**Architecture:** A dependency-free, thread-safe `LazyProviderProxy` preserves the compatibility-sensitive `_fs` and `client` module globals. The Flask authentication boundaries use explicit lock-backed Firebase getters, while the standalone auth service builds its legacy MSAL fallback only for a legacy pending-flow record; pytest no longer substitutes production constructors during collection.

**Tech Stack:** Python 3.12+, `threading.Lock`, Firebase Admin SDK, Google Cloud Firestore, OpenAI Python SDK, MSAL, Flask, `unittest.mock`, pytest subprocess collection.

**Design:** `docs/superpowers/specs/2026-08-13-backend-lazy-provider-initialization-design.md`

---

## File structure

- Create `email_automation/lazy_provider.py`: pure one-process lazy proxy; no provider imports.
- Create `tests/test_lazy_provider.py`: concurrency, failure retry, and no-introspection-initialization contract.
- Create `tests/test_runtime_provider_initialization.py`: import, first-use, health, Firebase, and MSAL startup regressions.
- Modify `email_automation/clients.py`: lazy Firestore/OpenAI globals.
- Modify `scheduler_runner.py`: lazy Firestore/OpenAI globals and runtime-only credential validation.
- Modify `app.py`: request-time Firebase initialization.
- Modify `auth_service/auth_service.py`: request-time Firebase and legacy-only MSAL pair.
- Modify `tests/test_scheduler_user_listing.py`: retain direct `_fs` replacement and add no-eager-construction regression.
- Modify `tests/test_surface_c_dashboard_auth.py`: Firebase first-request and failure behavior.
- Modify `tests/test_surface_c_device_flow.py`: patch the legacy pair getter rather than an eager app.
- Modify `auth_service/test_auth_service_isolation.py`: keep per-user isolation and assert no legacy fallback for new flows.
- Modify `tests/test_process_user_service.py`: exact health-with-zero-provider proof.
- Modify `tests/test_test_collection_contract.py`: no-conftest, zero-constructor complete-inventory gate.
- Delete `conftest.py`: remove the test-only provider substitution that masks production imports.

### Task 0: Freeze the partial baseline and reproduce the real RED

**Files:**
- Inspect: `conftest.py`
- Inspect: `tests/test_test_collection_contract.py`
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
`--noconftest` before `--collect-only`, add `openai.OpenAI` to the sitecustomize
boundary blockers, and remove the test that approves constructor replacement.
Do not delete `conftest.py` yet.

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

The guard must add:

```python
import openai
openai.OpenAI = _blocked_boundary("openai.OpenAI")
```

- [ ] **Step 4: Run the contract and capture the expected RED**

Run the Step 2 command again.

Expected: FAIL with at least one
`COLLECTION_EFFECT_ATTEMPTED:<constructor>` entry. The inherited `conftest.py`
must not make this subprocess pass because `--noconftest` is explicit.

- [ ] **Step 5: Commit only the stronger failing contract**

```bash
git add tests/test_test_collection_contract.py
git commit -m "test: expose eager provider construction during collection"
```

### Task 1: Build the lazy proxy with concurrency and retry guarantees

**Files:**
- Create: `email_automation/lazy_provider.py`
- Create: `tests/test_lazy_provider.py`

- [ ] **Step 1: Write the proxy RED**

Create tests with this complete behavioral core:

```python
from concurrent.futures import ThreadPoolExecutor
import threading
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
            return instance

        proxy = LazyProviderProxy("test", factory)
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: proxy.get(), range(64)))

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
```

Also test that `proxy.collection("users")` delegates to the created object and
that the proxy is truthy before initialization.

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

Run the Step 2 command.

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
- Modify: `tests/test_scheduler_user_listing.py`

- [ ] **Step 1: Add failing import and first-use tests**

In `tests/test_runtime_provider_initialization.py`, run each import in a fresh
subprocess with constructor and socket blockers installed before the import.
Assert importing `email_automation.clients` and `scheduler_runner` prints
`IMPORT_OK` and produces an empty boundary log. Add an in-process test that
reloads `email_automation.clients` under patched constructors and asserts:

```python
with patch.object(clients.firestore, "Client", return_value=fake_fs) as fs_ctor, \
     patch.object(clients.openai, "OpenAI", return_value=fake_ai) as ai_ctor:
    clients = importlib.reload(clients)
    fs_ctor.assert_not_called()
    ai_ctor.assert_not_called()
    self.assertIs(fake_fs.collection.return_value, clients._fs.collection("users"))
    self.assertIs(fake_ai.responses, clients.client.responses)
    fs_ctor.assert_called_once_with()
    ai_ctor.assert_called_once_with(api_key=clients.OPENAI_API_KEY)
```

Add equivalent constructor-count coverage for `scheduler_runner`. Retain a
test that `patch.object(module, "_fs", fake_fs)` intercepts listing unchanged.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_provider_initialization.py \
  tests/test_scheduler_user_listing.py
```

Expected: eager Firestore/OpenAI constructor assertions fail.

- [ ] **Step 3: Replace the eager objects in `clients.py`**

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

- [ ] **Step 4: Defer scheduler validation and constructors**

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

Call `_require_runtime_config()` as the first statement of
`refresh_and_process_user()` and as the first statement inside the `__main__`
block. Remove the import-time `openai.api_key` mutation. Do not touch the legacy
send disablement.

- [ ] **Step 5: Run focused GREEN and patch-seam regressions**

Run the Step 2 command.

Expected: all tests pass with constructors blocked during import, one
constructor per first use, and direct `_fs` replacement still effective.

- [ ] **Step 6: Commit the two production sites**

```bash
git add email_automation/clients.py scheduler_runner.py \
  tests/test_runtime_provider_initialization.py \
  tests/test_scheduler_user_listing.py
git commit -m "fix: defer backend provider clients until runtime use"
```

### Task 3: Move Firebase Admin initialization to authenticated request time

**Files:**
- Modify: `app.py`
- Modify: `auth_service/auth_service.py`
- Modify: `tests/test_surface_c_dashboard_auth.py`
- Modify: `tests/test_surface_c_device_flow.py`
- Modify: `tests/test_runtime_provider_initialization.py`

- [ ] **Step 1: Write failing Firebase timing tests**

For `app.py`, use the existing authenticated `GET /api/list-optouts` route. For
the auth service, use `POST /start-device-flow`. Assert:

```python
with patch.object(firebase_admin, "initialize_app") as initialize:
    imported = importlib.reload(appmod)
    initialize.assert_not_called()
    response = imported.app.test_client().get("/api/list-optouts")
    self.assertEqual(401, response.status_code)
    initialize.assert_not_called()
```

Load the auth service with the same `importlib.util.spec_from_file_location`
helper already used by `tests/test_surface_c_device_flow.py`, then make the
equivalent unauthenticated call:

```python
response = authmod.app.test_client().post("/start-device-flow", json={})
self.assertEqual(401, response.status_code)
initialize.assert_not_called()
```

Then patch `get_app` to raise `ValueError` before the first valid Bearer request,
patch `initialize_app` to succeed, and patch `verify_id_token` to a test uid.
Assert initialization once and protected handler behavior unchanged. Add a
failure-then-retry case: the first initialization raises and returns the exact
401 `Authentication unavailable`; the next request succeeds and initializes.

- [ ] **Step 2: Run the Firebase-focused tests and verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_runtime_provider_initialization.py \
  tests/test_surface_c_dashboard_auth.py \
  tests/test_surface_c_device_flow.py
```

Expected: import-time `initialize_app` calls violate the new assertions.

- [ ] **Step 3: Implement the local getters**

In each Flask module, retain the defensive SDK import but remove
`initialize_app()` from the import block. Add exactly:

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

In each decorator, call the getter only after validating a nonempty Bearer
token. Catch getter exceptions separately, log only the exception type, and
return the module's existing `Authentication unavailable` 401 shape. Call
`verify_id_token` on the returned module and retain every existing uid and
revocation check.

- [ ] **Step 4: Run Firebase GREEN**

Run the Step 2 command.

Expected: imports and unauthenticated requests construct nothing; valid first
request initializes once; failure is fail-closed and retryable.

- [ ] **Step 5: Commit the Firebase boundary**

```bash
git add app.py auth_service/auth_service.py \
  tests/test_surface_c_dashboard_auth.py \
  tests/test_surface_c_device_flow.py \
  tests/test_runtime_provider_initialization.py
git commit -m "fix: initialize Firebase only at authenticated boundaries"
```

### Task 4: Make the auth-service legacy MSAL fallback lazy and isolated

**Files:**
- Modify: `auth_service/auth_service.py`
- Modify: `auth_service/test_auth_service_isolation.py`
- Modify: `tests/test_surface_c_device_flow.py`
- Modify: `tests/test_runtime_provider_initialization.py`

- [ ] **Step 1: Add the MSAL RED**

First modernize only the stale auth-service test harness. Its fake Firebase
verifier must derive uid from the test token, every route call must carry a
Bearer token, and the TTL test must use the production names:

```python
def auth_headers(uid):
    return {"Authorization": f"Bearer {uid}"}


class FakeFirebaseAuth:
    @staticmethod
    def verify_id_token(token):
        return {"uid": token}


# In the fake firebase_admin module used during import:
fake_firebase_admin.get_app = lambda: object()
fake_firebase_admin.initialize_app = Mock(return_value=object())
fake_firebase_admin.auth = FakeFirebaseAuth

# Route examples:
self.client.post("/start-device-flow", json={}, headers=auth_headers("userA"))
self.mod.flows["userA"]["ts"] -= self.mod._FLOW_TTL_SECONDS + 1
```

Update stale expected error text to the current route contract, but do not
weaken the one-user/one-cache assertions. After that harness-only correction,
add tests proving module import calls `PublicClientApplication` zero times, two
new user flows create two distinct isolated apps/caches and never call the
legacy getter, and two legacy completions reuse one pair. Add this partial-entry
refutation, defining `legacy_factory` as a patch of
`authmod._get_legacy_msal_pair` and `upload` as a patch of
`authmod.upload_token`:

```python
authmod.flows["uid"] = {
    "flow": dict(FAKE_FLOW),
    "app": isolated_app,
    "ts": time.time(),
}
response = client.post("/complete-device-flow", json={}, headers=AUTH)
self.assertEqual(400, response.status_code)
legacy_factory.assert_not_called()
upload.assert_not_called()
```

Add a constructor-failure test that gets a generic 500 on the first legacy
completion and proves a second request retries the factory rather than reusing a
failed state.

- [ ] **Step 2: Run the auth-service suites and verify RED**

```bash
.venv/bin/python -m pytest -q -p no:cacheprovider \
  auth_service/test_auth_service_isolation.py \
  tests/test_surface_c_device_flow.py \
  tests/test_runtime_provider_initialization.py
```

Expected: eager module-global MSAL construction or missing getter assertions
fail. If the inherited auth-service isolation suite has unrelated pre-existing
failures, record them separately and require the exact new tests to RED for the
expected reason before changing production code.

- [ ] **Step 3: Implement `_get_legacy_msal_pair()`**

Delete eager `cache` and `msal_app`. Add the exact double-checked lock code from
the design. In `complete_flow()`:

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

Leave `_new_isolated_app()` and its exact single-account check unchanged.

- [ ] **Step 4: Update old patch seams explicitly**

Tests that formerly patched `authmod.msal_app` must patch
`authmod._get_legacy_msal_pair` or seed a complete isolated entry. Do not add a
compatibility global that would initialize on test introspection.

- [ ] **Step 5: Run MSAL GREEN and commit**

Run the Step 2 command. Expected: all new isolation/timing tests pass and every
new user still receives a distinct pair.

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
- Modify: `tests/test_process_user_service.py`

- [ ] **Step 1: Delete the provider-substitution hook**

Delete `conftest.py`. Remove
`test_provider_client_boundary_is_collection_only_and_restored` and every
`MagicMock`/`ExitStack` import used only by that workaround. Keep the
`--noconftest` subprocess option as a defense against a future masking hook.

- [ ] **Step 2: Strengthen the boundary and inventory assertions**

The sitecustomize guard must record and reject:

```text
socket.socket.connect
socket.create_connection
firestore.Client
firebase_admin.initialize_app
msal.PublicClientApplication
openai.OpenAI
```

Keep credential variables absent, use an empty temporary home, require
`boundary_calls == []`, require exit 0/no `INTERNALERROR`/no `SystemExit`, match
the reported count to every emitted node ID, and require both the auth-service
tests and collection-contract tests in the inventory. Do not hard-code 2,640;
the exact count may legitimately increase with the new tests, while node-ID
equality proves completeness.

- [ ] **Step 3: Pin the Phase 1 health contract with blocked constructors**

In `tests/test_process_user_service.py`, import `service` in a fresh subprocess
with all constructor blockers, issue `GET /health` and `GET /healthz` through
the Flask test client, and assert both exact bodies:

```python
assert health.status_code == 200
assert health.get_json() == {"status": "ok"}
assert healthz.status_code == 200
assert healthz.get_json() == {"status": "ok"}
```

The constructor log must remain empty. Do not call `/process-user`.

- [ ] **Step 4: Run the acceptance gate GREEN**

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
    tests/test_process_user_service.py
```

Expected: all focused tests pass, subprocess collection exits 0 with a complete
nonzero inventory, and no constructor/network boundary is logged.

- [ ] **Step 5: Commit the honest gate**

```bash
git add tests/test_test_collection_contract.py tests/test_process_user_service.py
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
