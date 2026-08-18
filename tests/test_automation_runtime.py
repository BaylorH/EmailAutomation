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


if __name__ == "__main__":
    unittest.main()
