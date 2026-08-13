# Backend lazy provider initialization design

Status: planning-only proposal for backlog #84; no runtime implementation is
authorized by this document.

## Decision and evidence

Use a small, thread-safe lazy proxy for the two compatibility-sensitive module
globals (`_fs` and `client`), and use explicit request-time getters for Firebase
Admin and the auth service's legacy MSAL fallback. Do not convert either Flask
module into a full application factory.

This design was derived from exact local partial head
`c1ba4714381d26c6eef5c8f1a2a2a8b8bff67a30`, whose release ancestor is
`6caa8ec14cc525299cfb8ed13bdd219f35c4322b`. The partial branch proves that
2,640 tests can be inventoried when pytest replaces provider constructors, but
that replacement hides five production import-time constructions instead of
removing them. FDR-042 therefore leaves #84 open.

The implementation deliverable is source and tests only. It performs no
provider call, network call, mailbox action, send, deployment, configuration
change, or production mutation. Both campaign switches and every Phase 2 hold
remain outside this work.

## Observed import and compatibility graph

| Import-time site | Import path and runtime callers | Existing test seam | Required migration |
| --- | --- | --- | --- |
| `email_automation/clients.py:13` `firestore.Client()` | `main.py` imports `_fs`, `list_user_ids`, and `decode_token_payload`; `service.py` imports `main`; `scheduler_lease.py` imports `_fs`; `app.py`, recovery scripts, and many domain modules import or patch `_fs` | A large test surface replaces `email_automation.clients._fs` directly; `main.py` uses the object imported at module load | Preserve `_fs` as an attribute-compatible proxy. Its first real method access constructs one Firestore client per process. |
| `scheduler_runner.py:63` `firestore.Client()` | Only legacy scheduler/listing and escalation tests import this module; the live Cloud Run Job uses `main.py` | Tests replace `scheduler_runner._fs` directly | Preserve `_fs` as the same proxy type. Do not unify this legacy singleton with `email_automation.clients._fs` in this change. |
| `app.py:32` `firebase_admin.initialize_app()` | The optional local Flask admin/API imports at least 15 route-contract suites | Tests patch `firebase_admin.auth.verify_id_token`; no caller requires initialization during import | Import the SDK module without initializing it. Resolve and initialize Firebase only after a syntactically valid Bearer token reaches the decorator. |
| `auth_service/auth_service.py:17` `firebase_admin.initialize_app()` | The standalone device-flow Flask service and its two test suites | Tests patch `firebase_admin.auth.verify_id_token` | Use the same request-time Firebase pattern locally in this standalone module. Keep it self-contained so an auth-service-only deployment does not depend on the `email_automation` package. |
| `auth_service/auth_service.py:48` `PublicClientApplication(...)` | Used only as a fallback for pending flow records created before per-user app isolation | `tests/test_surface_c_device_flow.py` patches module-global `msal_app`; identity-isolation tests fake the MSAL module | Replace the global app/cache with `_get_legacy_msal_pair()`. New flows continue to receive one isolated app/cache per identity and never use the fallback. |

Two adjacent client objects occur in files already in scope:
`email_automation/clients.py:15` and `scheduler_runner.py:60` construct
`openai.OpenAI` at import. Although the first zero-constructor probe did not
instrument that symbol, leaving them eager would make a claim of "no production
client construction during collection" incomplete. The same proxy migrates
them, and the final guard instruments `openai.OpenAI` too. This adds no new
runtime caller migration because existing code already accesses `client.files`
or `client.responses` by attribute and tests replace the whole `client` global.

## Approaches considered

### A. Compatibility proxy plus explicit request getters — recommended

Create one dependency-free `LazyProviderProxy` and retain `_fs`/`client` at
their current module names. Firebase and the legacy MSAL pair use explicit
getters because they have only a few internal callers and failure must be
translated at the authentication boundary.

Advantages:

- The live `main.py -> clients._fs` and `scheduler_lease.py -> clients._fs`
  bindings keep working without a broad caller rewrite.
- Existing tests that patch the whole `_fs` or `client` module global keep the
  same seam.
- Constructor timing changes, but provider identity, constructor arguments,
  and one-client-per-process behavior do not.
- A pure utility module introduces no reverse import and therefore no circular
  dependency.

The trade-off is that `_fs` is a proxy rather than a literal Firestore `Client`
until first use. Repository search found no production `isinstance`, identity,
pickling, or special-method dependency on that literal type. Attribute access,
including `collection()` and `transaction()`, is the required contract.

### B. Replace every global with `get_firestore_client()` calls

This is explicit, but it requires changing `main.py`, `scheduler_lease.py`,
`app.py`, scripts, and hundreds of test patches. It creates a much larger
production diff merely to solve import timing and makes rollback harder. Reject
for this bounded correction.

### C. Convert both Flask modules and the worker to application factories

Dependency injection through `create_app(dependencies=...)` is architecturally
clean for a new service. Here, `app.py` has more than 3,200 lines of routes
registered against a module-global Flask object, `service.py` must continue to
export `service:app`, and the auth service has a distinct standalone deployment
shape. A factory conversion would mix route registration, deployment entrypoint,
and behavior changes into #84. Reject and preserve the module-level Flask apps.

## Exact APIs

### Attribute-compatible provider proxy

Create `email_automation/lazy_provider.py` with this public surface and
implementation contract:

```python
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

    def get(self) -> T:
        instance = self._instance
        if instance is _UNSET:
            with self._lock:
                instance = self._instance
                if instance is _UNSET:
                    instance = self._factory()
                    self._instance = instance
        return cast(T, instance)

    @property
    def initialized(self) -> bool:
        return self._instance is not _UNSET

    def __getattr__(self, attribute: str):
        return getattr(self.get(), attribute)

    def __repr__(self) -> str:
        state = "ready" if self.initialized else "uninitialized"
        return f"LazyProviderProxy(name={self._name!r}, state={state})"
```

`get()` performs a double-checked lookup around one `threading.Lock`. The
factory runs while the lock is held, and `_instance` is assigned only after the
factory returns successfully. An exception is not cached; the initiating caller
receives the original exception and a later caller may retry. `__getattr__`
delegates only ordinary attribute access to `get()`. `__repr__` and
`initialized` never initialize. The proxy remains truthy without defining
`__bool__`, preserving code such as `fs_client = fs_client or _fs`.

The factory must not call back into the same proxy. The scoped factories are
leaf constructors, so this rule prevents recursive initialization without an
`RLock` that could hide recursion. Every process owns its own proxy and client;
there is no cross-process object sharing under gunicorn or Cloud Run.

### Firestore and OpenAI globals

The two production modules retain their existing names:

```python
_fs = LazyProviderProxy(
    "email_automation.clients.firestore",
    lambda: firestore.Client(),
)
client = LazyProviderProxy(
    "email_automation.clients.openai",
    lambda: openai.OpenAI(api_key=OPENAI_API_KEY),
)
```

`scheduler_runner.py` uses distinct names in the diagnostic string but the same
shape. Lambdas deliberately resolve the module attribute at first use rather
than capturing a constructor at import, so focused tests can patch
`firestore.Client` or `openai.OpenAI` before the first access. Remove the
import-time `openai.api_key = ...` mutations.

The existing scheduler import-time credential exceptions move to
`_require_runtime_config()`. It is called at the beginning of the legacy
`refresh_and_process_user()` entry and immediately inside the `__main__` block,
before listing users or touching a provider. The proxy factories also call this
validator before constructing OpenAI or Firestore, so direct legacy helpers
cannot bypass the former fail-fast check. Import alone never makes the choice.
The exception texts remain byte-for-byte identical.

### Firebase Admin getters

`app.py` and `auth_service/auth_service.py` each define a local lock and getter
because the auth service must remain standalone:

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

The import block imports modules only; it does not initialize an app. The
decorator validates that `Authorization` contains a nonempty Bearer token before
calling the getter. Getter or initialization failure returns the existing
fail-closed 401 `Authentication unavailable` response and does not call
`verify_id_token` or the protected route. Verification failure keeps the
existing `Invalid authentication token` 401. Initialization failures are not
cached, so a later request can recover after credentials become available.

The lock covers the `get_app`/`initialize_app` check as one critical section.
That avoids two threads both observing no default app. The SDK registry remains
the source of truth, so a default app initialized elsewhere is reused.

### Legacy auth-service MSAL pair

Replace the import-time `cache` and `msal_app` globals with:

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

`complete_flow()` uses `entry["app"]` and `entry["cache"]` when both are
present. Only a legacy entry missing both calls `_get_legacy_msal_pair()`. A
partially shaped entry fails closed as invalid rather than mixing one isolated
object with one shared object. Construction failure leaves the pair unset,
logs only the exception type, and returns the existing generic 500. The next
request may retry. `_new_isolated_app()` is unchanged: each newly authenticated
user still receives a fresh cache and app, preserving the wrong-mailbox guard.

## Per-site migration and behavior

1. `email_automation/clients.py`: replace eager Firestore and OpenAI objects
   with proxies. All `_fs.collection(...)` calls and exported names stay
   unchanged.
2. `scheduler_runner.py`: replace eager clients, defer its credential check to
   actual legacy runtime entry/use, and otherwise leave the disabled legacy send
   boundary untouched.
3. `app.py`: remove `initialize_app()` from the import block and resolve auth in
   `verify_firebase_token` only after a Bearer token is present. Do not alter
   route registration, CORS, scheduler availability, or Flask entrypoints.
4. `auth_service/auth_service.py`: make Firebase request-lazy and make only the
   legacy MSAL fallback process-lazy. Do not change per-user flow isolation,
   flow TTL/cap, recipient/mailbox identity, upload behavior, or response body
   contracts.
5. `conftest.py`: delete the collection-time provider substitutions. A test
   suite must not make broken production imports appear safe.
6. `tests/test_test_collection_contract.py`: run the subprocess with
   `--noconftest`, block the four constructor symbols plus sockets, require an
   empty boundary log, and retain the complete node-ID/inventory assertions.

## Tests and falsification

The implementation is accepted only if all of these are green without real
credentials:

- A fresh proxy constructs once under concurrent first access, returns the same
  object, does not initialize for `repr`/`initialized`, and retries after one
  factory exception.
- Import probes for `email_automation.clients`, `scheduler_runner`, `app.py`, and
  `auth_service/auth_service.py` record zero Firestore, Firebase, MSAL, OpenAI,
  socket, or HTTP boundary calls.
- Repository-wide `pytest --collect-only --noconftest` returns every node ID,
  including auth-service and collection-contract tests, with no credentials and
  an empty constructor/network log. No test hook replaces provider constructors.
- `service:app` imports with constructors blocked, and both `GET /health` and
  `GET /healthz` retain exact `200 {"status":"ok"}` bodies without initializing
  any provider.
- First Firestore/OpenAI use invokes its real constructor seam once; a failed
  first constructor is observable and a later use retries.
- Missing or malformed Bearer tokens initialize no Firebase app. A valid-token
  request initializes once across concurrent calls, reuses an existing default
  app, and fails closed before the protected handler when initialization fails.
- New auth-service device flows construct only isolated per-user MSAL pairs.
  Only explicitly legacy entries construct/reuse the fallback pair; partial
  entries and constructor failures fail closed.
- Existing scheduler listing, scheduler lease, process-user service, dashboard
  auth, device-flow, and identity-isolation suites remain green with fake
  providers and zero external effects.

The inherited `auth_service/test_auth_service_isolation.py` is not a green
baseline at `c1ba471`: seven tests still call authenticated routes without a
Bearer token and refer to the superseded `created`/`_PENDING_TTL_SECONDS`
names, while the current service uses `ts`/`_FLOW_TTL_SECONDS`. The
implementation plan modernizes that test harness only—fake token verification,
current field names, and current response contract—before using it as MSAL
evidence. That pre-existing test defect is not evidence against lazy
initialization and must not be hidden by claiming the inherited suite passed.

Evidence refuting the design includes any constructor call during import or
collection, any missing node ID, more than one construction under concurrent
first use, a cached constructor failure, a provider call on `/health`, a change
to exact health/auth response contracts, a new-flow use of the legacy MSAL
pair, or a direct patch seam that no longer intercepts runtime use.

## Rollout and rollback

Implementation happens on a new source branch descended from the partial
collection work. It is reviewed and pushed only after focused and full hermetic
verification. This design authorizes no merge or deployment.

If a later separately approved release discovers a startup regression, revert
the bounded implementation commits together. That restores the known eager
construction behavior and reopens #84; it does not require data repair because
the change creates no schema, document, token, queue, or provider-side state.
Do not add a runtime flag that can silently restore eager imports: a flag would
let collection pass in one mode while production runs another and would defeat
the invariant.

## Scope boundaries

This work does not unify the duplicate legacy scheduler, redesign token-cache
storage, alter Graph/Firestore/Sheets/OpenAI calls after initialization, change
mailbox or user scope, introduce app factories, enable `/process-user`, change
campaign switches, send mail, or address Phase 2. Import-time file creation in
the optional legacy Flask scheduler bootstrap and unrelated manual executables
remain outside this constructor correction unless the strengthened
`--noconftest` probe directly refutes collection; if it does, stop and write a
separate scope amendment rather than silently expanding the patch.
