"""Request-scoped runtime isolation and counter atomicity.

Task 5 of the production automation certification plan.

Two properties carry this module. First, ISOLATION: two concurrent runtimes must
share nothing, because a certification run and an ordinary production run can be
in flight at the same moment in the same process, and a shared capture, clock,
counter, source, transport, run id, or fixture scope is how a fixture effect
escapes into production - or worse, how a production effect gets counted as
certification evidence.

Second, COUNTER ATOMICITY: send caps are the last line of defence against
mailing a broker twice. A reservation must be all-or-nothing across user AND
global scope, idempotent under retry, and refundable exactly once and only after
a send is PROVEN not to have happened. An ambiguous delivery must RETAIN its
reservation - releasing on ambiguity is how a duplicate send gets authorised.

This module imports only `automation_runtime`, which is pure by construction, so
it collects with no credential.
"""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest

from email_automation import automation_runtime as ar
from email_automation.message_transport import (
    CanonicalConversationState,
    HydratedInboundMessage,
)


class RuntimeIsolationTests(unittest.TestCase):
    """Two runtimes share nothing."""

    def test_two_certification_runtimes_share_no_dependency(self):
        # Snapshots are supplied so the source fields hold REAL objects. Comparing
        # two None values would pass vacuously and prove nothing about sharing.
        snapshot = {"id": "m1", "body": {"contentType": "Text", "content": "hi"}}
        conversation = {"reply_target": snapshot, "prior_messages": [], "sent_receipts": []}
        left = ar.certification_runtime(
            run_id="run-a", scope="fixture-a",
            inbound_snapshot=snapshot, conversation_snapshot=conversation,
        )
        right = ar.certification_runtime(
            run_id="run-b", scope="fixture-b",
            inbound_snapshot=snapshot, conversation_snapshot=conversation,
        )

        for field in (
            "inbound",
            "conversations",
            "outbound",
            "counters",
            "effect_scope",
            "ai_provider",
            "drive_publication",
        ):
            with self.subTest(field=field):
                self.assertIsNot(
                    getattr(left, field),
                    getattr(right, field),
                    f"{field} is shared between two runtimes",
                )

    def test_run_identity_and_scope_are_distinct(self):
        left = ar.certification_runtime(run_id="run-a", scope="fixture-a")
        right = ar.certification_runtime(run_id="run-b", scope="fixture-b")
        self.assertEqual(left.certification_run_id, "run-a")
        self.assertEqual(right.certification_run_id, "run-b")
        self.assertNotEqual(left.certification_scope, right.certification_scope)

    def test_runtime_is_immutable(self):
        runtime = ar.certification_runtime(run_id="run-a", scope="fixture-a")
        with self.assertRaises(Exception):
            runtime.certification_run_id = "tampered"  # type: ignore[misc]

    def test_captures_do_not_leak_between_runtimes(self):
        left = ar.certification_runtime(run_id="run-a", scope="fixture-a")
        right = ar.certification_runtime(run_id="run-b", scope="fixture-b")
        left.drive_publication.publish("file-1", {"role": "reader", "type": "anyone"})
        self.assertEqual(len(left.drive_publication.captured), 1)
        self.assertEqual(len(right.drive_publication.captured), 0)

    def test_production_runtime_is_not_certification(self):
        runtime = ar.production_runtime()
        self.assertIsNone(runtime.certification_run_id)
        self.assertIsNone(runtime.certification_scope)


class ProductionDefaultTests(unittest.TestCase):
    """Omitted dependencies mean ordinary production, resolved lazily."""

    def test_omitted_dependencies_resolve_to_production_factories(self):
        runtime = ar.production_runtime()
        self.assertIs(runtime.ai_provider.__class__, ar.ProviderBackedAITransport)
        self.assertIs(
            runtime.drive_publication.__class__, ar.ProviderBackedDrivePublication
        )

    def test_production_defaults_construct_no_provider_at_build_time(self):
        """Building a runtime must not construct a client.

        If it did, merely describing a runtime would need credentials and would
        reintroduce the import-time provider construction this program exists to
        remove (backlog #84).
        """
        runtime = ar.production_runtime()
        self.assertFalse(runtime.ai_provider.is_resolved())
        self.assertFalse(runtime.drive_publication.is_resolved())

    def test_explicit_override_wins_over_the_default(self):
        sentinel = ar.DenyingAITransport(reason="custom")
        runtime = ar.production_runtime(ai_provider=sentinel)
        self.assertIs(runtime.ai_provider, sentinel)

    def test_runtime_cannot_be_built_from_public_request_data(self):
        """Arbitrary runtime construction must not be reachable from a request."""
        for payload in (
            {"ai_provider": "anything"},
            {"counters": {"limit": 10**9}},
            {"effect_scope": None},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ar.RuntimeConstructionError):
                    ar.runtime_from_request(payload)


class CertificationTransportTests(unittest.TestCase):
    """Certification denies real provider effects and captures would-be ones."""

    def test_ai_inference_raises_user_runtime_launch_required_before_any_request(self):
        runtime = ar.certification_runtime(run_id="run-a", scope="fixture-a")
        for method, args in (
            ("create_response", ({"model": "gpt-5.2"},)),
            ("create_chat_completion", ({"model": "gpt-5.2"},)),
            ("upload_file", (object(), "assistants")),
        ):
            with self.subTest(method=method):
                with self.assertRaises(ar.UserRuntimeLaunchRequired) as ctx:
                    getattr(runtime.ai_provider, method)(*args)
                self.assertIn("user_runtime_launch_required", str(ctx.exception))

    def test_denied_ai_records_no_request_payload(self):
        """A denial must not retain the prompt; evidence keeps digests, not bodies."""
        runtime = ar.certification_runtime(run_id="run-a", scope="fixture-a")
        with self.assertRaises(ar.UserRuntimeLaunchRequired):
            runtime.ai_provider.create_response({"model": "gpt-5.2", "input": "secret"})
        self.assertNotIn("secret", repr(runtime.ai_provider.attempts))

    def test_drive_publication_captures_and_never_creates_a_permission(self):
        runtime = ar.certification_runtime(run_id="run-a", scope="fixture-a")
        body = {"role": "reader", "type": "anyone"}
        result = runtime.drive_publication.publish("file-1", body)
        self.assertEqual(result["status"], "captured")
        self.assertEqual(runtime.drive_publication.captured, [("file-1", body)])
        self.assertEqual(runtime.drive_publication.real_permission_calls, 0)

    def test_drive_publication_validates_exact_file_and_body(self):
        runtime = ar.certification_runtime(run_id="run-a", scope="fixture-a")
        for file_id, body in (("", {"role": "reader"}), ("file-1", None), ("file-1", {})):
            with self.subTest(file_id=file_id, body=body):
                with self.assertRaises(ValueError):
                    runtime.drive_publication.publish(file_id, body)


class EffectScopeTests(unittest.TestCase):
    """EffectScope enforces boundaries and contains no business rules."""

    def _scope(self):
        return ar.FixtureEffectScope(
            firestore_prefix="certification/run-a",
            sheet_ids=("sheet-fixture",),
            drive_parents=("folder-fixture",),
        )

    def test_firestore_path_outside_the_prefix_is_refused(self):
        scope = self._scope()
        scope.assert_firestore_path("certification/run-a/threads/t1")
        with self.assertRaises(ar.EffectScopeViolation):
            scope.assert_firestore_path("users/real-user/threads/t1")

    def test_sheet_target_outside_the_fixture_set_is_refused(self):
        scope = self._scope()
        scope.assert_sheet_target("sheet-fixture", "A1:B2")
        with self.assertRaises(ar.EffectScopeViolation):
            scope.assert_sheet_target("customer-sheet", "A1:B2")

    def test_drive_parent_outside_the_fixture_set_is_refused(self):
        scope = self._scope()
        scope.assert_drive_parent("folder-fixture")
        with self.assertRaises(ar.EffectScopeViolation):
            scope.assert_drive_parent("customer-folder")

    def test_public_drive_permission_is_always_refused(self):
        scope = self._scope()
        with self.assertRaises(ar.EffectScopeViolation):
            scope.assert_drive_permission("file-1", {"type": "anyone", "role": "reader"})


class CounterReservationTests(unittest.TestCase):
    """All-or-nothing across user AND global, idempotent, exactly refundable."""

    def _store(self, user_limit=5, global_limit=8):
        return ar.InMemoryCounterStore(
            limits={("user", "u1"): user_limit, ("global", "all"): global_limit}
        )

    def _reservations(self, amount):
        return (
            ar.CounterReservation(scope="user", key="u1", amount=amount, limit=5),
            ar.CounterReservation(scope="global", key="all", amount=amount, limit=8),
        )

    def test_reservation_is_all_or_nothing_across_both_scopes(self):
        store = self._store(user_limit=10, global_limit=1)
        token = store.reserve_many(self._reservations(2), idempotency_key="k1")
        self.assertIsNone(token, "global scope cannot fit, so nothing may be reserved")
        self.assertEqual(store.used("user", "u1"), 0, "partial reservation is a defect")
        self.assertEqual(store.used("global", "all"), 0)

    def test_multi_message_amounts_are_reserved_together(self):
        store = self._store()
        token = store.reserve_many(self._reservations(3), idempotency_key="k1")
        self.assertIsNotNone(token)
        self.assertEqual(store.used("user", "u1"), 3)
        self.assertEqual(store.used("global", "all"), 3)

    def test_same_key_retry_returns_the_original_token_without_incrementing(self):
        store = self._store()
        first = store.reserve_many(self._reservations(2), idempotency_key="k1")
        second = store.reserve_many(self._reservations(2), idempotency_key="k1")
        self.assertEqual(first, second)
        self.assertEqual(store.used("user", "u1"), 2, "a retry must not double-count")

    def test_limit_is_enforced_under_concurrency(self):
        store = self._store(user_limit=4, global_limit=4)
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(
                pool.map(
                    lambda i: store.reserve_many(
                        self._reservations(1), idempotency_key=f"k{i}"
                    ),
                    range(8),
                )
            )
        granted = [token for token in tokens if token is not None]
        self.assertEqual(len(granted), 4, "exactly the limit may be granted")
        self.assertEqual(store.used("user", "u1"), 4)

    def test_refund_by_token_is_exact_and_idempotent(self):
        store = self._store()
        token = store.reserve_many(self._reservations(2), idempotency_key="k1")
        assert token is not None
        store.release_many(token)
        self.assertEqual(store.used("user", "u1"), 0)
        store.release_many(token)
        self.assertEqual(store.used("user", "u1"), 0, "double release is a zero delta")

    def test_cross_token_release_is_refused(self):
        store = self._store()
        real = store.reserve_many(self._reservations(2), idempotency_key="k1")
        assert real is not None
        forged = ar.CounterReservationToken(
            reservation_id="not-mine", reservations=real.reservations
        )
        with self.assertRaises(ar.CounterReservationError):
            store.release_many(forged)
        self.assertEqual(store.used("user", "u1"), 2, "a forged release must not refund")

    def test_over_refund_is_impossible(self):
        store = self._store()
        first = store.reserve_many(self._reservations(2), idempotency_key="k1")
        second = store.reserve_many(self._reservations(1), idempotency_key="k2")
        assert first is not None and second is not None
        store.release_many(first)
        store.release_many(first)
        store.release_many(second)
        self.assertEqual(store.used("user", "u1"), 0)
        self.assertGreaterEqual(store.used("user", "u1"), 0, "usage may never go negative")

    def test_ambiguous_delivery_retains_the_reservation(self):
        """Releasing on ambiguity is how a duplicate send gets authorised."""
        store = self._store()
        token = store.reserve_many(self._reservations(1), idempotency_key="k1")
        assert token is not None
        store.record_outcome(token, outcome="ambiguous")
        self.assertEqual(store.used("user", "u1"), 1)
        with self.assertRaises(ar.CounterReservationError):
            store.release_many(token)
        self.assertEqual(store.used("user", "u1"), 1)

    def test_confirmed_send_keeps_the_reservation(self):
        store = self._store()
        token = store.reserve_many(self._reservations(1), idempotency_key="k1")
        assert token is not None
        store.record_outcome(token, outcome="sent")
        self.assertEqual(store.used("user", "u1"), 1)
        with self.assertRaises(ar.CounterReservationError):
            store.release_many(token)

    def test_proven_no_send_releases_exactly_once(self):
        store = self._store()
        token = store.reserve_many(self._reservations(1), idempotency_key="k1")
        assert token is not None
        store.record_outcome(token, outcome="not_sent")
        store.release_many(token)
        self.assertEqual(store.used("user", "u1"), 0)
        store.release_many(token)
        self.assertEqual(store.used("user", "u1"), 0)


class RuntimeSourceTests(unittest.TestCase):
    """The runtime carries the Task 3 canonical sources, unchanged."""

    def test_certification_runtime_uses_fixture_sources(self):
        snapshot = {"id": "m1", "body": {"contentType": "Text", "content": "hi"}}
        runtime = ar.certification_runtime(
            run_id="run-a", scope="fixture-a", inbound_snapshot=snapshot
        )
        hydrated = runtime.inbound.hydrate({"id": "m1"})
        self.assertIsInstance(hydrated, HydratedInboundMessage)
        self.assertEqual(hydrated.full_text, "hi")

    def test_certification_conversation_source_returns_canonical_state(self):
        runtime = ar.certification_runtime(
            run_id="run-a",
            scope="fixture-a",
            conversation_snapshot={
                "reply_target": {"id": "m1", "body": {"contentType": "Text", "content": "hi"}},
                "prior_messages": [],
                "sent_receipts": [],
            },
        )
        state = runtime.conversations.load("conv-1")
        self.assertIsInstance(state, CanonicalConversationState)
        self.assertEqual(state.prior_messages, ())


# ---------------------------------------------------------------------------
# Task 5A - scoped data clients
# ---------------------------------------------------------------------------
#
# The fence exists because the product reaches its data stores through long
# chains: ``fs.collection(..).document(..).collection(..).stream()`` and then
# ``snapshot.reference.delete()``. Guarding only the entry point is worthless -
# ONE unwrapped return value anywhere in that chain is a full escape back onto
# the ambient production client, and the escape is silent, because the product
# wraps almost every store call in ``except Exception``.
#
# So these tests do not merely check that a violation raises. They walk every
# chain a first-slice call graph can produce and require that EVERY returned
# object is still fenced, and then deliberately break each wrap point in turn to
# prove the walk actually catches a leak - before the provider is ever invoked.


class _RawBase:
    """A raw provider object. Escaping the fence means one of these gets out."""

    def __init__(self, provider, path):
        self.provider = provider
        self.path = path


class _RawSnapshot:
    def __init__(self, provider, path, data, exists=True):
        self.provider = provider
        self.path = path
        self.id = path.rsplit("/", 1)[-1]
        self._data = dict(data)
        self.exists = exists

    def to_dict(self):
        return dict(self._data)

    @property
    def reference(self):
        return _RawDocument(self.provider, self.path)


class _RawDocument(_RawBase):
    @property
    def id(self):
        return self.path.rsplit("/", 1)[-1]

    def collection(self, name):
        return _RawCollection(self.provider, f"{self.path}/{name}")

    def get(self, transaction=None):
        self.provider.reads.append(self.path)
        return _RawSnapshot(self.provider, self.path, self.provider.data.get(self.path, {}))

    def set(self, data, merge=False):
        self.provider.mutations.append(("set", self.path, data, merge))

    def update(self, data):
        self.provider.mutations.append(("update", self.path, data, None))

    def create(self, data):
        self.provider.mutations.append(("create", self.path, data, None))

    def delete(self):
        self.provider.mutations.append(("delete", self.path, None, None))


class _RawCollection(_RawBase):
    def document(self, name):
        return _RawDocument(self.provider, f"{self.path}/{name}")

    def where(self, *args, **kwargs):
        return _RawCollection(self.provider, self.path)

    def order_by(self, *args, **kwargs):
        return _RawCollection(self.provider, self.path)

    def limit(self, *args, **kwargs):
        return _RawCollection(self.provider, self.path)

    def add(self, data):
        self.provider.mutations.append(("add", self.path, data, None))
        return _RawDocument(self.provider, f"{self.path}/generated")

    def stream(self):
        self.provider.reads.append(self.path)
        for child in self.provider.children.get(self.path, ()):
            yield _RawSnapshot(
                self.provider,
                f"{self.path}/{child}",
                self.provider.data.get(f"{self.path}/{child}", {}),
            )

    def get(self):
        return list(self.stream())


class _RawTransaction:
    def __init__(self, provider):
        self.provider = provider
        self._max_attempts = 1
        self._read_only = False
        self._id = b"raw"

    def _clean_up(self):
        return None

    def _begin(self, retry_id=None):
        return None

    def _commit(self):
        return []

    def _rollback(self):
        return None

    def _assert_raw(self, ref):
        if not isinstance(ref, _RawDocument):
            raise AssertionError(
                "the fence handed the provider something that is not a raw "
                f"document reference: {type(ref).__name__}"
            )

    def set(self, ref, data, merge=False):
        self._assert_raw(ref)
        self.provider.mutations.append(("txn-set", ref.path, data, merge))

    def update(self, ref, data):
        self._assert_raw(ref)
        self.provider.mutations.append(("txn-update", ref.path, data, None))

    def create(self, ref, data):
        self._assert_raw(ref)
        self.provider.mutations.append(("txn-create", ref.path, data, None))

    def delete(self, ref):
        self._assert_raw(ref)
        self.provider.mutations.append(("txn-delete", ref.path, None, None))


class _RawBatch(_RawTransaction):
    def commit(self):
        self.provider.mutations.append(("batch-commit", "", None, None))
        return []


class _RawFirestore:
    def __init__(self):
        self.mutations = []
        self.reads = []
        self.data = {}
        self.children = {}

    def collection(self, name):
        return _RawCollection(self, name)

    def transaction(self, **kwargs):
        return _RawTransaction(self)

    def batch(self):
        return _RawBatch(self)


class _RawSheetRequest:
    def __init__(self, provider, label, kwargs):
        self.provider = provider
        self.label = label
        self.kwargs = kwargs

    def execute(self):
        self.provider.executed.append((self.label, self.kwargs))
        return {"ok": True}


class _RawSheetValues:
    def __init__(self, provider):
        self.provider = provider

    def get(self, **kwargs):
        return _RawSheetRequest(self.provider, "values.get", kwargs)

    def update(self, **kwargs):
        return _RawSheetRequest(self.provider, "values.update", kwargs)

    def batchUpdate(self, **kwargs):
        return _RawSheetRequest(self.provider, "values.batchUpdate", kwargs)


class _RawSpreadsheets:
    def __init__(self, provider):
        self.provider = provider

    def values(self):
        return _RawSheetValues(self.provider)

    def get(self, **kwargs):
        return _RawSheetRequest(self.provider, "spreadsheets.get", kwargs)

    def batchUpdate(self, **kwargs):
        return _RawSheetRequest(self.provider, "spreadsheets.batchUpdate", kwargs)


class _RawSheets:
    def __init__(self):
        self.executed = []

    def spreadsheets(self):
        return _RawSpreadsheets(self)


FENCE_SCOPE_PREFIX = "users/cert-uid"
FENCE_SHEET_ID = "sheet-fixture-1"


class ScopedClientContractTests(unittest.TestCase):
    """The fence must survive every chain the first slice can build."""

    def setUp(self):
        self.provider = _RawFirestore()
        self.provider.children["users/cert-uid/threads/t1/messages"] = ("m1", "m2")
        self.provider.data["users/cert-uid/threads/t1/messages/m1"] = {"direction": "outbound"}
        self.provider.data["users/cert-uid/threads/t1/messages/m2"] = {"direction": "inbound"}
        self.scope = ar.FixtureEffectScope(
            firestore_prefix=FENCE_SCOPE_PREFIX,
            sheet_ids=(FENCE_SHEET_ID,),
        )
        self.fs = ar.ScopedFirestore(self.provider, self.scope)
        self.sheets_provider = _RawSheets()
        self.sheets = ar.ScopedSheets(self.sheets_provider, self.scope)

    # -- in-scope behavior is ordinary ------------------------------------

    def test_in_scope_write_reaches_the_provider(self):
        (
            self.fs.collection("users").document("cert-uid")
            .collection("outbox").document("o1")
            .set({"status": "queued"}, merge=True)
        )
        self.assertEqual(
            self.provider.mutations,
            [("set", "users/cert-uid/outbox/o1", {"status": "queued"}, True)],
        )

    def test_in_scope_read_returns_a_fenced_snapshot(self):
        self.provider.data["users/cert-uid"] = {"email": "fixture@example.invalid"}
        snapshot = self.fs.collection("users").document("cert-uid").get()
        self.assertIsInstance(snapshot, ar.ScopedSnapshot)
        self.assertEqual(snapshot.to_dict(), {"email": "fixture@example.invalid"})
        self.assertIsInstance(snapshot.reference, ar.ScopedDocument)

    # -- out-of-scope is refused, and the refusal survives a bare except ---

    def test_out_of_scope_write_is_refused_before_the_provider(self):
        doc = self.fs.collection("users").document("someone-else").collection("outbox").document("o1")
        with self.assertRaises(ar.EffectScopeViolation):
            doc.set({"status": "queued"})
        self.assertEqual(self.provider.mutations, [])

    def test_out_of_scope_read_is_refused_before_the_provider(self):
        doc = self.fs.collection("users").document("someone-else")
        with self.assertRaises(ar.EffectScopeViolation):
            doc.get()
        self.assertEqual(self.provider.reads, [])

    def test_a_refused_effect_is_recorded_even_when_the_caller_swallows_it(self):
        """Product code wraps store calls in ``except Exception``.

        A raised violation alone is therefore NOT sufficient evidence: the
        product would swallow it, log a warning, and return False, and the run
        would look merely unlucky rather than out of scope. The scope keeps its
        own record so a swallowed violation is still visible in evidence.
        """
        doc = self.fs.collection("users").document("someone-else")
        try:
            doc.delete()
        except Exception:  # exactly what the product does
            pass
        self.assertEqual(len(self.scope.violations), 1)
        self.assertIn("users/someone-else", self.scope.violations[0])
        self.assertEqual(self.provider.mutations, [])

    # -- the read-only global allowance -----------------------------------

    def test_a_named_global_document_is_readable_but_never_writable(self):
        """Campaign authority reads ``systemConfig/campaignAccess``.

        It lives outside every per-user subtree, so a prefix-only fence refuses
        it - and because the product treats an unreadable policy as UNKNOWN and
        fails closed, the run would suppress its own send and look like an
        ordinary requeue. Readable, never writable, and exact-match only.
        """
        scope = ar.FixtureEffectScope(
            firestore_prefix=FENCE_SCOPE_PREFIX,
            readable_paths=("systemConfig/campaignAccess",),
        )
        fs = ar.ScopedFirestore(self.provider, scope)
        policy = fs.collection("systemConfig").document("campaignAccess")

        self.assertIsInstance(policy.get(), ar.ScopedSnapshot)
        self.assertEqual(scope.violations, [])

        for write in (
            lambda: policy.set({"enabled": True}),
            lambda: policy.update({"enabled": True}),
            lambda: policy.delete(),
        ):
            with self.assertRaises(ar.EffectScopeViolation):
                write()
        self.assertEqual(self.provider.mutations, [])

    def test_the_allowance_is_exact_match_not_a_prefix(self):
        scope = ar.FixtureEffectScope(
            firestore_prefix=FENCE_SCOPE_PREFIX,
            readable_paths=("systemConfig/campaignAccess",),
        )
        fs = ar.ScopedFirestore(self.provider, scope)
        for path in ("systemConfig", "systemConfig/campaignAccessOther"):
            with self.subTest(path=path):
                node = fs.collection("systemConfig")
                doc = node.document(path.split("/", 1)[1]) if "/" in path else None
                with self.assertRaises(ar.EffectScopeViolation):
                    (doc.get() if doc is not None else list(node.stream()))

    def test_no_allowance_means_the_global_read_is_refused(self):
        with self.assertRaises(ar.EffectScopeViolation):
            self.fs.collection("systemConfig").document("campaignAccess").get()

    # -- every chain stays fenced -----------------------------------------

    def _walk_every_chain(self):
        """Walk each chain the first slice builds; assert each hop is fenced.

        Returns nothing - it raises on the first unwrapped object it meets.
        """
        def check(obj, expected, where):
            if not isinstance(obj, expected):
                raise AssertionError(
                    f"{where} returned {type(obj).__name__}, which is OUTSIDE the "
                    "fence: the ambient provider is reachable through it"
                )
            return obj

        root = check(self.fs.collection("users"), ar.ScopedCollection, "firestore.collection")
        user = check(root.document("cert-uid"), ar.ScopedDocument, "collection.document")
        threads = check(user.collection("threads"), ar.ScopedCollection, "document.collection")
        thread = check(threads.document("t1"), ar.ScopedDocument, "collection.document")
        messages = check(thread.collection("messages"), ar.ScopedCollection, "document.collection")

        query = check(messages.where("direction", "==", "outbound"), ar.ScopedQuery, "collection.where")
        query = check(query.order_by("createdAt"), ar.ScopedQuery, "query.order_by")
        query = check(query.limit(10), ar.ScopedQuery, "query.limit")

        for snapshot in check(list(query.stream()), list, "query.stream"):
            check(snapshot, ar.ScopedSnapshot, "query.stream element")
            check(snapshot.reference, ar.ScopedDocument, "snapshot.reference")

        for snapshot in messages.stream():
            check(snapshot, ar.ScopedSnapshot, "collection.stream element")
            check(snapshot.reference, ar.ScopedDocument, "snapshot.reference")

        check(user.get(), ar.ScopedSnapshot, "document.get")
        check(messages.get(), list, "collection.get")
        check(self.fs.transaction(), ar.ScopedTransaction, "firestore.transaction")
        check(self.fs.batch(), ar.ScopedBatch, "firestore.batch")

    def test_every_returned_object_stays_inside_the_fence(self):
        self._walk_every_chain()

    def test_generated_document_from_add_stays_inside_the_fence(self):
        messages = (
            self.fs.collection("users").document("cert-uid")
            .collection("threads").document("t1").collection("messages")
        )
        self.assertIsInstance(messages.add({"a": 1}), ar.ScopedDocument)
        with self.assertRaises(ar.EffectScopeViolation):
            self.fs.collection("users").document("someone-else").collection("x").add({"a": 1})

    def test_breaking_any_single_wrap_point_is_caught_before_the_provider(self):
        """The mutation check the plan asks for.

        For each wrap point, make it return ONE raw provider object and require
        the walk to fail - and to fail with no provider mutation having run.
        """
        leaks = [
            (ar.ScopedFirestore, "collection", lambda self, name: self._inner.collection(name)),
            (ar.ScopedCollection, "document", lambda self, name: self._inner.document(name)),
            (ar.ScopedDocument, "collection", lambda self, name: self._inner.collection(name)),
            (ar.ScopedCollection, "where", lambda self, *a, **k: self._inner.where(*a, **k)),
            (ar.ScopedQuery, "order_by", lambda self, *a, **k: self._inner.order_by(*a, **k)),
            (ar.ScopedQuery, "limit", lambda self, *a, **k: self._inner.limit(*a, **k)),
            (ar.ScopedCollection, "stream", lambda self: iter(list(self._inner.stream()))),
            (ar.ScopedQuery, "stream", lambda self: iter(list(self._inner.stream()))),
            (ar.ScopedDocument, "get", lambda self, transaction=None: self._inner.get()),
            (ar.ScopedSnapshot, "reference", property(lambda self: self._inner.reference)),
            (ar.ScopedFirestore, "transaction", lambda self, **k: self._inner.transaction()),
            (ar.ScopedFirestore, "batch", lambda self: self._inner.batch()),
        ]
        for cls, name, leak in leaks:
            with self.subTest(wrap_point=f"{cls.__name__}.{name}"):
                self.setUp()
                with patch.object(cls, name, leak):
                    with self.assertRaises(AssertionError):
                        self._walk_every_chain()
                self.assertEqual(
                    self.provider.mutations, [],
                    "the leak reached the provider before the walk caught it",
                )

    # -- transactions, batches, retry callbacks ---------------------------

    def test_transaction_writes_are_fenced_and_unwrapped_for_the_provider(self):
        txn = self.fs.transaction()
        good = self.fs.collection("users").document("cert-uid")
        txn.set(good, {"a": 1}, merge=True)
        self.assertEqual(
            self.provider.mutations, [("txn-set", "users/cert-uid", {"a": 1}, True)]
        )

        bad = self.fs.collection("users").document("someone-else")
        for call in (
            lambda: txn.set(bad, {"a": 1}),
            lambda: txn.update(bad, {"a": 1}),
            lambda: txn.create(bad, {"a": 1}),
            lambda: txn.delete(bad),
        ):
            with self.assertRaises(ar.EffectScopeViolation):
                call()
        self.assertEqual(len(self.provider.mutations), 1)

    def test_transaction_refuses_an_unfenced_reference(self):
        """A raw ref reaching a fenced transaction is an escape, not a shortcut."""
        txn = self.fs.transaction()
        raw = self.provider.collection("users").document("cert-uid")
        with self.assertRaises(ar.EffectScopeViolation):
            txn.set(raw, {"a": 1})
        self.assertEqual(self.provider.mutations, [])

    def test_batch_writes_are_fenced(self):
        batch = self.fs.batch()
        batch.set(self.fs.collection("users").document("cert-uid"), {"a": 1})
        with self.assertRaises(ar.EffectScopeViolation):
            batch.delete(self.fs.collection("users").document("someone-else"))
        batch.commit()
        self.assertEqual(
            [entry[0] for entry in self.provider.mutations], ["txn-set", "batch-commit"]
        )

    def test_retry_callback_transaction_is_still_fenced(self):
        """``@firestore.transactional`` hands the callback the transaction.

        ``notifications.delete_notification_and_decrement_counters`` reaches the
        store exactly this way, so the fence has to survive the decorator's
        private protocol as well as its public one.
        """
        from google.cloud import firestore as gfirestore

        notif = self.fs.collection("users").document("cert-uid").collection("notifications").document("n1")
        outside = self.fs.collection("users").document("someone-else")

        @gfirestore.transactional
        def run(transaction):
            transaction.delete(notif)
            return "done"

        self.assertEqual(run(self.fs.transaction()), "done")
        self.assertEqual(
            self.provider.mutations,
            [("txn-delete", "users/cert-uid/notifications/n1", None, None)],
        )

        @gfirestore.transactional
        def run_outside(transaction):
            transaction.delete(outside)

        with self.assertRaises(ar.EffectScopeViolation):
            run_outside(self.fs.transaction())

    def test_snapshot_reference_delete_is_fenced(self):
        """messaging._delete_synthetic_outbound_duplicates deletes via .reference."""
        streamed = list(
            self.fs.collection("users").document("cert-uid")
            .collection("threads").document("t1").collection("messages").stream()
        )
        streamed[0].reference.delete()
        self.assertEqual(
            self.provider.mutations,
            [("delete", "users/cert-uid/threads/t1/messages/m1", None, None)],
        )

    # -- sheets -----------------------------------------------------------

    def test_sheet_values_calls_are_fenced_by_target(self):
        values = self.sheets.spreadsheets().values()
        values.get(spreadsheetId=FENCE_SHEET_ID, range="Sheet1!A1:Z1").execute()
        self.assertEqual(len(self.sheets_provider.executed), 1)

        with self.assertRaises(ar.EffectScopeViolation):
            values.get(spreadsheetId="not-the-fixture", range="Sheet1!A1:Z1")
        with self.assertRaises(ar.EffectScopeViolation):
            values.get(spreadsheetId=FENCE_SHEET_ID, range="")
        self.assertEqual(len(self.sheets_provider.executed), 1)

    def test_sheet_value_writes_are_fenced_by_target_and_body(self):
        values = self.sheets.spreadsheets().values()
        values.update(
            spreadsheetId=FENCE_SHEET_ID,
            range="Sheet1!A2",
            body={"values": [["x"]]},
            valueInputOption="RAW",
        ).execute()
        with self.assertRaises(ar.EffectScopeViolation):
            values.update(spreadsheetId="other", range="Sheet1!A2", body={"values": []})
        with self.assertRaises(ar.EffectScopeViolation):
            values.update(spreadsheetId=FENCE_SHEET_ID, range="Sheet1!A2", body={})
        self.assertEqual(len(self.sheets_provider.executed), 1)

    def test_numeric_grid_batch_update_is_fenced_by_typed_request_not_a1(self):
        """Row highlighting has no A1 range at all - only a numeric grid body.

        Validating A1 ranges alone would wave this straight through, which is
        why ``EffectScope`` carries ``assert_sheet_request``.
        """
        highlight = {
            "requests": [
                {"repeatCell": {"range": {"sheetId": 0, "startRowIndex": 4, "endRowIndex": 5}}}
            ]
        }
        self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=FENCE_SHEET_ID, body=highlight
        ).execute()
        self.assertEqual(len(self.sheets_provider.executed), 1)

        with self.assertRaises(ar.EffectScopeViolation):
            self.sheets.spreadsheets().batchUpdate(spreadsheetId="other", body=highlight)
        with self.assertRaises(ar.EffectScopeViolation):
            self.sheets.spreadsheets().batchUpdate(spreadsheetId=FENCE_SHEET_ID, body={})
        self.assertEqual(len(self.sheets_provider.executed), 1)

    def test_spreadsheet_metadata_read_is_fenced(self):
        self.sheets.spreadsheets().get(spreadsheetId=FENCE_SHEET_ID).execute()
        with self.assertRaises(ar.EffectScopeViolation):
            self.sheets.spreadsheets().get(spreadsheetId="other")
        self.assertEqual(len(self.sheets_provider.executed), 1)

    def test_breaking_a_sheets_wrap_point_is_caught_before_execute(self):
        leaks = [
            (ar.ScopedSheets, "spreadsheets", lambda self: self._inner.spreadsheets()),
            (ar.ScopedSpreadsheets, "values", lambda self: self._inner.values()),
        ]
        for cls, name, leak in leaks:
            with self.subTest(wrap_point=f"{cls.__name__}.{name}"):
                self.sheets_provider = _RawSheets()
                self.sheets = ar.ScopedSheets(self.sheets_provider, self.scope)
                with patch.object(cls, name, leak):
                    surface = self.sheets.spreadsheets()
                    leaked = not isinstance(surface, ar.ScopedSpreadsheets)
                    if not leaked:
                        leaked = not isinstance(surface.values(), ar.ScopedValues)
                    self.assertTrue(leaked, "the leak was not observable")
                self.assertEqual(self.sheets_provider.executed, [])

    # -- drive ------------------------------------------------------------

    def test_drive_is_deny_all_in_the_first_slice(self):
        drive = ar.DenyingDriveClient()
        for attribute in ("files", "permissions", "about"):
            with self.subTest(attribute=attribute):
                with self.assertRaises(ar.EffectScopeViolation):
                    getattr(drive, attribute)

    # -- the runtime wires the fence, and production does not --------------

    def test_certification_runtime_wraps_supplied_providers(self):
        runtime = ar.certification_runtime(
            run_id="run-a",
            scope="fixture-a",
            firestore=self.provider,
            sheets=self.sheets_provider,
            firestore_prefix=FENCE_SCOPE_PREFIX,
            sheet_ids=(FENCE_SHEET_ID,),
        )
        self.assertIsInstance(runtime.firestore, ar.ScopedFirestore)
        self.assertIsInstance(runtime.sheets, ar.ScopedSheets)
        self.assertIsInstance(runtime.drive, ar.DenyingDriveClient)

    def test_production_runtime_leaves_clients_ambient(self):
        """Omitting a client must still mean 'ordinary production'."""
        runtime = ar.production_runtime()
        self.assertIsNone(runtime.firestore)
        self.assertIsNone(runtime.sheets)
        self.assertIsNone(runtime.drive)

    def test_client_resolution_prefers_the_runtime_and_falls_back_to_ambient(self):
        ambient = object()
        self.assertIs(ar.firestore_for(None, ambient), ambient)
        self.assertIs(ar.firestore_for(ar.production_runtime(), ambient), ambient)
        runtime = ar.certification_runtime(
            run_id="run-a", scope="fixture-a",
            firestore=self.provider, firestore_prefix=FENCE_SCOPE_PREFIX,
        )
        self.assertIs(ar.firestore_for(runtime, ambient), runtime.firestore)


if __name__ == "__main__":
    unittest.main()
