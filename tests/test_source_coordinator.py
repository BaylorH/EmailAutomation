import hashlib
import importlib
import importlib.util
import inspect
import json
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread
from unittest import mock

from tests.source_coordinator_fakes import (
    FakeDocumentSnapshot,
    FakeFirestore,
    MutableClock,
)


MODULE_NAME = "email_automation.source_coordinator"
FROZEN_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
CLASSIFICATION_INPUT = {
    "schemaVersion": 1,
    "message": {
        "from": "sender@example.test",
        "subject": "Availability question",
        "body": "Is the property still available?",
    },
}
COMPLETE_PROPOSAL = {
    "schemaVersion": 1,
    "transitionCandidates": [
        {"type": "needs_user_input", "reason": "availability_review"}
    ],
    "ordinaryObligations": [
        {"type": "field_update", "field": "last_contacted"}
    ],
}
MODEL_PROPOSAL_EVIDENCE = {
    "schemaVersion": 1,
    "evidenceKind": "model_capture",
    "responseHash": "a" * 64,
}
HARD_OPTOUT_EVIDENCE = {
    "schemaVersion": 1,
    "evidenceKind": "header_list_unsubscribe",
    "evidenceHash": "b" * 64,
}


def _load_source_coordinator(test_case):
    spec = importlib.util.find_spec(MODULE_NAME)
    test_case.assertIsNotNone(spec, "source coordinator module is missing")
    if spec is None:
        return None
    return importlib.import_module(MODULE_NAME)


class MalformedMode(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.equality_calls = 0
        return instance

    def __eq__(self, other):
        self.equality_calls += 1
        return other == "enforced"

    __hash__ = str.__hash__


class HostileString(str):
    def __eq__(self, other):
        return True

    def strip(self, *args, **kwargs):
        raise AssertionError("hostile strip executed")

    def encode(self, *args, **kwargs):
        raise AssertionError("hostile encode executed")

    __hash__ = str.__hash__


class JsonMappingSubclass(dict):
    pass


class JsonListSubclass(list):
    pass


class SequentialUUIDs:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return f"source-{self.calls:04d}"


class CorruptingReadbackFirestore(FakeFirestore):
    """Hide one applied document only during commit-error readback."""

    def __init__(self):
        super().__init__()
        self.hidden_readback_path = None

    def _snapshot(self, document_ref):
        snapshot = super()._snapshot(document_ref)
        apply_error_seen = any(
            event[0] == "commit_raised_after_apply" for event in self.events
        )
        if apply_error_seen and document_ref.path == self.hidden_readback_path:
            return FakeDocumentSnapshot(document_ref, None)
        return snapshot


class SourceCoordinatorContractTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = _load_source_coordinator(self)

    def test_public_error_codes_are_stable(self):
        coordinator = self.coordinator
        expected_codes = {
            "SourceCoordinatorError": "source_coordinator_error",
            "SourceCoordinatorRetryable": "source_coordinator_retryable",
            "SourceCoordinatorAmbiguous": "source_coordinator_ambiguous",
            "SourceCoordinatorConflict": "source_coordinator_conflict",
            "SourceCoordinatorConfigError": "source_coordinator_config",
        }
        base_error = coordinator.SourceCoordinatorError
        self.assertTrue(issubclass(base_error, RuntimeError))
        for name, code in expected_codes.items():
            with self.subTest(name=name):
                error_type = getattr(coordinator, name)
                self.assertTrue(issubclass(error_type, base_error))
                self.assertEqual(code, error_type.code)

    def test_mode_defaults_disabled_and_unknown_fails_disabled(self):
        coordinator = self.coordinator
        mode = coordinator.CoordinatorMode
        self.assertEqual(
            ["disabled", "shadow", "enforced"],
            [item.value for item in mode],
        )
        self.assertIs(
            mode.DISABLED,
            coordinator.resolve_source_coordinator_mode({}),
        )
        self.assertIs(
            mode.SHADOW,
            coordinator.resolve_source_coordinator_mode(
                {"SITESIFT_SOURCE_COORDINATOR_MODE": "shadow"}
            ),
        )
        self.assertIs(
            mode.ENFORCED,
            coordinator.resolve_source_coordinator_mode(
                {"SITESIFT_SOURCE_COORDINATOR_MODE": "enforced"}
            ),
        )
        for invalid in ("typo", "SHADOW", " shadow ", "", None):
            with self.subTest(invalid=invalid):
                self.assertIs(
                    mode.DISABLED,
                    coordinator.resolve_source_coordinator_mode(
                        {"SITESIFT_SOURCE_COORDINATOR_MODE": invalid}
                    ),
                )

        malformed = MalformedMode("garbage")
        self.assertIs(
            mode.DISABLED,
            coordinator.resolve_source_coordinator_mode(
                {"SITESIFT_SOURCE_COORDINATOR_MODE": malformed}
            ),
        )
        self.assertEqual(0, malformed.equality_calls)

    def test_source_alias_is_frozen_and_limit_is_exact(self):
        coordinator = self.coordinator
        alias = coordinator.SourceAlias("graph", "opaque")
        self.assertEqual(("graph", "opaque", ""), (alias.alias_type, alias.value, alias.key))
        self.assertEqual(1024, coordinator.MAX_SOURCE_ALIAS_BYTES)
        with self.assertRaises(FrozenInstanceError):
            alias.value = "changed"

    def test_canonical_json_hash_uses_sorted_compact_finite_json(self):
        coordinator = self.coordinator
        value = {"z": [True, None, 2.5], "a": "café"}
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected = hashlib.sha256(encoded).hexdigest()
        actual = coordinator.canonical_json_hash(value)
        self.assertEqual(expected, actual)
        self.assertEqual(64, len(actual))

    def test_canonical_json_hash_translates_invalid_values_to_config_error(self):
        coordinator = self.coordinator
        invalid_values = (
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": float("-inf")},
            {"value": object()},
            {"value": {"mutable-set"}},
        )
        cyclic = []
        cyclic.append(cyclic)
        invalid_values += (cyclic,)
        for value in invalid_values:
            with self.subTest(value=repr(value)), self.assertRaises(
                coordinator.SourceCoordinatorConfigError
            ):
                coordinator.canonical_json_hash(value)

    def test_canonical_json_hash_rejects_non_exact_json_collision_shapes(self):
        coordinator = self.coordinator
        invalid_values = (
            {1: "x"},
            (1, 2),
            {"nested": (1, 2)},
            {"nested": JsonMappingSubclass({"key": "value"})},
            {"nested": JsonListSubclass([1, 2])},
            {"\ud800": "invalid surrogate key"},
            {"value": "\ud800"},
        )
        for value in invalid_values:
            with self.subTest(value=repr(value)), self.assertRaises(
                coordinator.SourceCoordinatorConfigError
            ):
                coordinator.canonical_json_hash(value)

    def test_alias_normalization_preserves_opaque_case(self):
        coordinator = self.coordinator
        graph = coordinator.normalize_source_alias("graph", "  AbC+/=  ")
        rfc = coordinator.normalize_source_alias(
            "internet_message_id", " <<Case@Example.TEST>> "
        )
        self.assertEqual(
            coordinator.SourceAlias("graph", "AbC+/="),
            graph,
        )
        self.assertEqual(
            coordinator.SourceAlias("internet_message_id", "Case@Example.TEST"),
            rfc,
        )

    def test_alias_normalization_rejects_invalid_values(self):
        coordinator = self.coordinator
        invalid_aliases = (
            ("unknown", "value"),
            (None, "value"),
            (HostileString("graph"), "value"),
            ("graph", None),
            ("graph", 123),
            ("graph", HostileString("value")),
            ("graph", ""),
            ("graph", "   "),
            ("graph", "abc\x00def"),
            ("graph", "abc\ndef"),
            ("internet_message_id", "<<>>"),
            ("graph", "é" * 513),
            ("graph", "a" * (coordinator.MAX_SOURCE_ALIAS_BYTES + 1)),
        )
        for alias_type, value in invalid_aliases:
            with self.subTest(alias_type=alias_type, value=repr(value)), self.assertRaises(
                coordinator.SourceCoordinatorConfigError
            ):
                coordinator.normalize_source_alias(alias_type, value)

        boundary = "a" * coordinator.MAX_SOURCE_ALIAS_BYTES
        self.assertEqual(
            boundary,
            coordinator.normalize_source_alias("graph", boundary).value,
        )

    def test_source_alias_key_is_full_domain_separated_sha256(self):
        coordinator = self.coordinator
        alias = coordinator.normalize_source_alias("graph", "AbC+/=")
        expected = hashlib.sha256(
            b"source-alias-v2\0user-1\0graph\0AbC+/="
        ).hexdigest()
        key = coordinator.source_alias_key("user-1", alias)
        self.assertEqual(expected, key)
        self.assertEqual(64, len(key))

        variants = {
            coordinator.source_alias_key("user-2", alias),
            coordinator.source_alias_key(
                "user-1",
                coordinator.normalize_source_alias(
                    "internet_message_id", "AbC+/="
                ),
            ),
            coordinator.source_alias_key(
                "user-1", coordinator.normalize_source_alias("graph", "AbC+/=2")
            ),
        }
        self.assertEqual(3, len(variants))
        self.assertNotIn(key, variants)

    def test_source_alias_key_validates_user_and_canonical_alias(self):
        coordinator = self.coordinator
        canonical = coordinator.normalize_source_alias("graph", "opaque")
        invalid_inputs = (
            ("", canonical),
            (None, canonical),
            (123, canonical),
            (HostileString("user-1"), canonical),
            ("\ud800", canonical),
            ("user-1", coordinator.SourceAlias("graph", " opaque ")),
            (
                "user-1",
                coordinator.SourceAlias(HostileString("graph"), "opaque"),
            ),
            (
                "user-1",
                coordinator.SourceAlias("graph", HostileString("opaque")),
            ),
            ("user-1", coordinator.SourceAlias("unknown", "opaque")),
            ("user-1", object()),
        )
        for user_id, alias in invalid_inputs:
            with self.subTest(user_id=user_id, alias=repr(alias)), self.assertRaises(
                coordinator.SourceCoordinatorConfigError
            ):
                coordinator.source_alias_key(user_id, alias)


class SourceCoordinatorFakeTests(unittest.TestCase):
    def test_document_references_reject_only_firestore_reserved_ids(self):
        fake = FakeFirestore()
        collection = fake.collection("items")
        for document_id in ("__reserved__", "____"):
            with self.subTest(document_id=document_id), self.assertRaises(
                ValueError
            ):
                collection.document(document_id)

        for document_id in ("___", "__partial_", "_partial__"):
            with self.subTest(document_id=document_id):
                self.assertEqual(document_id, collection.document(document_id).id)

    def test_create_preconditions_are_atomic(self):
        fake = FakeFirestore()
        existing = fake.collection("items").document("existing")
        untouched = fake.collection("items").document("untouched")
        existing.create({"value": 1})
        before = dict(fake.data)

        transaction = fake.transaction()
        transaction.set(untouched, {"value": 2})
        transaction.create(existing, {"value": 3})
        with self.assertRaises(RuntimeError):
            transaction.commit()

        self.assertEqual(before, fake.data)
        self.assertFalse(untouched.get().exists)

    def test_transaction_rejects_reads_after_buffered_writes(self):
        fake = FakeFirestore()
        document = fake.collection("items").document("one")
        transaction = fake.transaction()
        transaction._begin()
        transaction.create(document, {"value": 1})
        with self.assertRaisesRegex(RuntimeError, "reads after writes"):
            transaction.get(document)

    def test_transaction_rejects_reference_from_another_store(self):
        first = FakeFirestore()
        second = FakeFirestore()
        foreign = second.collection("items").document("one")
        transaction = first.transaction()
        transaction._begin()
        with self.assertRaises(TypeError):
            foreign.get(transaction=transaction)

    def test_transaction_get_shape_matches_firestore_document_get_contract(self):
        fake = FakeFirestore()
        document = fake.collection("items").document("one")
        document.create({"value": 1})
        transaction = fake.transaction(max_attempts=1)
        transaction._begin()

        self.assertEqual(1, transaction._max_attempts)
        transaction_result = transaction.get(document)
        self.assertNotIsInstance(transaction_result, FakeDocumentSnapshot)
        self.assertEqual(
            {"value": 1},
            next(transaction_result).to_dict(),
        )
        self.assertEqual(
            {"value": 1},
            document.get(transaction=transaction).to_dict(),
        )

    def test_transaction_query_orders_and_detects_collection_phantoms(self):
        fake = FakeFirestore()
        items = fake.collection("items")
        items.document("later").create({"threadId": "thread-1", "rank": 2})
        items.document("earlier").create({"threadId": "thread-1", "rank": 1})
        items.document("other").create({"threadId": "thread-2", "rank": 0})
        transaction = fake.transaction(max_attempts=1)
        transaction._begin()
        query = items.where("threadId", "==", "thread-1").order_by("rank")

        snapshots = list(transaction.get(query))

        self.assertEqual(["earlier", "later"], [item.id for item in snapshots])
        items.document("phantom").create({"threadId": "thread-1", "rank": 3})
        with self.assertRaisesRegex(RuntimeError, "snapshot is stale"):
            transaction._commit()


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_source_coordinator(self)
        self.assertTrue(
            hasattr(self.module, "SourceCoordinator"),
            "SourceCoordinator is absent",
        )
        self.assertTrue(
            hasattr(self.module.SourceCoordinator, "admit_or_repair_source_identity"),
            "SourceCoordinator.admit_or_repair_source_identity is absent",
        )
        self.fake = FakeFirestore()
        self.uuids = SequentialUUIDs()
        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=lambda: FROZEN_NOW,
        )

    def admit(
        self,
        hydrated_message,
        *,
        evidence_kind="graph_hydration",
        thread_id="thread-1",
    ):
        return self.coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message=hydrated_message,
            evidence_kind=evidence_kind,
            thread_id=thread_id,
        )

    def assertErrorCode(self, expected_code, callable_):
        with self.assertRaises(self.module.SourceCoordinatorError) as raised:
            callable_()
        self.assertEqual(expected_code, raised.exception.code)
        return raised.exception

    def identity_path(self, source_id):
        return f"users/user-1/sourceIdentities/{source_id}"

    def alias_paths(self):
        prefix = "users/user-1/sourceAliases/"
        return sorted(path for path in self.fake.data if path.startswith(prefix))

    def write_events(self):
        return [
            event
            for event in self.fake.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def assertOnlyIdentityWrites(self):
        for event in self.write_events():
            self.assertRegex(
                event[1],
                r"^users/user-1/source(?:Identities|Aliases)/",
            )

    def test_graph_first_then_graph_and_rfc_enrichment_retains_identity(self):
        graph_only = {"id": " graph-A ", "conversationId": "conversation-1"}
        graph_and_rfc = {
            "id": "graph-A",
            "internetMessageId": "<<Case@Example.TEST>>",
            "conversationId": "conversation-1",
        }

        created = self.admit(graph_only)
        repaired = self.admit(graph_and_rfc)

        self.assertEqual("source-0001", created.canonical_source_id)
        self.assertTrue(created.created)
        self.assertFalse(created.repaired)
        self.assertEqual("source-0001", repaired.canonical_source_id)
        self.assertFalse(repaired.created)
        self.assertTrue(repaired.repaired)
        self.assertIsInstance(repaired.aliases, tuple)
        self.assertEqual(
            {("graph", "graph-A"), ("internet_message_id", "Case@Example.TEST")},
            {(alias.alias_type, alias.value) for alias in repaired.aliases},
        )
        self.assertEqual(1, self.uuids.calls)
        self.assertEqual(2, len(self.alias_paths()))
        self.assertOnlyIdentityWrites()

    def test_rfc_first_then_rfc_and_graph_enrichment_retains_identity(self):
        created = self.admit({"internetMessageId": "<rfc-1@example.test>"})
        repaired = self.admit(
            {"id": "graph-1", "internetMessageId": "rfc-1@example.test"}
        )

        self.assertEqual("source-0001", created.canonical_source_id)
        self.assertEqual("source-0001", repaired.canonical_source_id)
        self.assertEqual(1, self.uuids.calls)
        self.assertEqual(2, len(repaired.aliases))

    def test_bound_alias_wins_without_allocating_another_uuid(self):
        first = self.admit({"id": "graph-1"})
        self.assertEqual(1, self.uuids.calls)

        second = self.admit(
            {"id": "graph-1", "internetMessageId": "<rfc-1@example.test>"}
        )

        self.assertEqual(first.canonical_source_id, second.canonical_source_id)
        self.assertEqual(1, self.uuids.calls)

    def test_late_alias_requires_an_overlapping_trusted_envelope(self):
        graph = self.admit(
            {"id": "graph-1", "conversationId": "shared-conversation"}
        )
        disjoint_rfc = self.admit(
            {
                "internetMessageId": "<rfc-1@example.test>",
                "conversationId": "shared-conversation",
            }
        )

        self.assertNotEqual(
            graph.canonical_source_id,
            disjoint_rfc.canonical_source_id,
        )
        before = dict(self.fake.data)
        self.assertErrorCode(
            "source_alias_conflict",
            lambda: self.admit(
                {
                    "id": "graph-1",
                    "internetMessageId": "<rfc-1@example.test>",
                    "conversationId": "shared-conversation",
                }
            ),
        )
        self.assertEqual(before, self.fake.data)

    def test_two_owner_conflict_has_zero_writes(self):
        self.admit({"id": "graph-A"})
        self.admit({"internetMessageId": "<rfc-B@example.test>"})
        before_data = dict(self.fake.data)
        before_writes = list(self.write_events())

        self.assertErrorCode(
            "source_alias_conflict",
            lambda: self.admit(
                {
                    "id": "graph-A",
                    "internetMessageId": "<rfc-B@example.test>",
                }
            ),
        )

        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_rebound_alias_projection_is_rejected_without_writes(self):
        source = self.admit(
            {"id": "graph-A", "internetMessageId": "<rfc-A@example.test>"}
        )
        other = self.admit({"id": "graph-B"})
        rfc_alias = self.module.normalize_source_alias(
            "internet_message_id", "rfc-A@example.test"
        )
        rfc_key = self.module.source_alias_key("user-1", rfc_alias)
        alias_path = f"users/user-1/sourceAliases/{rfc_key}"
        self.fake.data[alias_path]["canonicalSourceId"] = other.canonical_source_id
        before_data = dict(self.fake.data)
        before_writes = list(self.write_events())

        self.assertNotEqual(source.canonical_source_id, other.canonical_source_id)
        self.assertErrorCode(
            "source_alias_conflict",
            lambda: self.admit(
                {"id": "graph-A", "internetMessageId": "rfc-A@example.test"}
            ),
        )
        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_same_owner_projection_absent_from_identity_is_explicitly_ambiguous(self):
        source = self.admit({"id": "graph-A"})
        rfc_alias = self.module.normalize_source_alias(
            "internet_message_id",
            "rfc-A@example.test",
        )
        rfc_key = self.module.source_alias_key("user-1", rfc_alias)
        rfc_ref = (
            self.fake.collection("users")
            .document("user-1")
            .collection("sourceAliases")
            .document(rfc_key)
        )
        rfc_ref.create(
            {
                "schemaVersion": 1,
                "sourceAliasKey": rfc_key,
                "aliasType": "internet_message_id",
                "normalizedValueHash": self.module.canonical_json_hash(
                    {
                        "schemaVersion": 1,
                        "hashKind": "source-alias-normalized-value-v1",
                        "aliasType": "internet_message_id",
                        "normalizedValue": "rfc-A@example.test",
                    }
                ),
                "canonicalSourceId": source.canonical_source_id,
                "createdAt": FROZEN_NOW,
            }
        )
        before_data = deepcopy(self.fake.data)
        before_writes = list(self.write_events())

        with self.assertRaisesRegex(
            self.module.SourceCoordinatorAmbiguous,
            "projection is absent from identity authority",
        ) as raised:
            self.admit(
                {
                    "id": "graph-A",
                    "internetMessageId": "<rfc-A@example.test>",
                }
            )

        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_conversation_and_internal_thread_ids_are_never_aliases(self):
        first = self.admit(
            {"id": "graph-A", "conversationId": "shared-routing-id"},
            thread_id="shared-thread-id",
        )
        second = self.admit(
            {"id": "graph-B", "conversationId": "shared-routing-id"},
            evidence_kind="operator_replay",
            thread_id="shared-thread-id",
        )

        self.assertNotEqual(first.canonical_source_id, second.canonical_source_id)
        self.assertEqual(2, len(self.alias_paths()))
        serialized_aliases = json.dumps(
            {path: self.fake.data[path] for path in self.alias_paths()},
            sort_keys=True,
            default=str,
        )
        self.assertNotIn("shared-routing-id", serialized_aliases)
        self.assertNotIn("shared-thread-id", serialized_aliases)
        before = dict(self.fake.data)
        self.assertErrorCode(
            "source_identity_missing",
            lambda: self.admit(
                {"conversationId": "shared-routing-id"},
                evidence_kind="operator_replay",
                thread_id="shared-thread-id",
            ),
        )
        self.assertEqual(before, self.fake.data)

    def test_nonempty_internal_thread_binding_is_immutable(self):
        created = self.admit({"id": "graph-A"}, thread_id=None)
        bound = self.admit({"id": "graph-A"}, thread_id="thread-A")
        identity_path = self.identity_path(created.canonical_source_id)
        self.assertEqual(created.canonical_source_id, bound.canonical_source_id)
        self.assertEqual("thread-A", self.fake.data[identity_path]["threadId"])
        before_data = dict(self.fake.data)
        before_writes = list(self.write_events())

        self.assertErrorCode(
            "source_thread_conflict",
            lambda: self.admit({"id": "graph-A"}, thread_id="thread-B"),
        )

        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_fail_before_apply_is_retryable_and_has_no_partial_identity(self):
        self.fake.fail_next_commit = RuntimeError("commit unavailable")

        self.assertErrorCode(
            "source_coordinator_retryable",
            lambda: self.admit(
                {"id": "graph-A", "internetMessageId": "<rfc-A@example.test>"}
            ),
        )

        self.assertEqual({}, self.fake.data)
        self.assertEqual([], self.write_events())
        self.assertEqual(
            1,
            sum(event[0] == "transaction_began" for event in self.fake.events),
        )

    def test_apply_then_raise_is_accepted_after_full_strict_readback(self):
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown commit")

        result = self.admit(
            {"id": "graph-A", "internetMessageId": "<rfc-A@example.test>"}
        )

        self.assertEqual("source-0001", result.canonical_source_id)
        self.assertTrue(result.created)
        self.assertIn(self.identity_path("source-0001"), self.fake.data)
        self.assertEqual(2, len(self.alias_paths()))
        self.assertEqual(
            1,
            sum(event[0] == "transaction_began" for event in self.fake.events),
        )

    def test_apply_then_raise_rejects_incomplete_readback_as_ambiguous(self):
        fake = CorruptingReadbackFirestore()
        uuids = SequentialUUIDs()
        coordinator = self.module.SourceCoordinator(
            fake,
            uuid_factory=uuids,
            now_factory=lambda: FROZEN_NOW,
        )
        graph_alias = self.module.normalize_source_alias("graph", "graph-A")
        graph_key = self.module.source_alias_key("user-1", graph_alias)
        fake.hidden_readback_path = f"users/user-1/sourceAliases/{graph_key}"
        fake.apply_then_raise_next_commit = RuntimeError("unknown commit")

        with self.assertRaises(self.module.SourceCoordinatorAmbiguous) as raised:
            coordinator.admit_or_repair_source_identity(
                user_id="user-1",
                hydrated_message={"id": "graph-A"},
                evidence_kind="graph_hydration",
                thread_id="thread-1",
            )

        self.assertEqual("source_coordinator_ambiguous", raised.exception.code)

    def test_transaction_factory_failures_are_typed_without_starting_writes(self):
        class RaisingTransactionFactory:
            def __init__(self, error):
                self.error = error
                self.calls = []

            def transaction(self, max_attempts=5):
                self.calls.append(max_attempts)
                raise self.error

        cases = (
            (
                "provider failure",
                RuntimeError("transaction factory unavailable"),
                self.module.SourceCoordinatorRetryable,
            ),
            (
                "typed failure",
                self.module.SourceCoordinatorConfigError("typed factory error"),
                self.module.SourceCoordinatorConfigError,
            ),
        )
        for case, factory_error, expected_type in cases:
            with self.subTest(case=case):
                uuids = SequentialUUIDs()
                firestore = RaisingTransactionFactory(factory_error)
                coordinator = self.module.SourceCoordinator(
                    firestore,
                    uuid_factory=uuids,
                    now_factory=lambda: FROZEN_NOW,
                )
                caught = None
                try:
                    coordinator.admit_or_repair_source_identity(
                        user_id="user-1",
                        hydrated_message={"id": "graph-A"},
                        evidence_kind="graph_hydration",
                        thread_id="thread-1",
                    )
                except Exception as exc:  # Assert taxonomy below without a raw error.
                    caught = exc

                self.assertIsInstance(caught, expected_type)
                if isinstance(factory_error, self.module.SourceCoordinatorError):
                    self.assertIs(factory_error, caught)
                else:
                    self.assertIs(factory_error, caught.__cause__)
                self.assertEqual([1], firestore.calls)
                self.assertEqual(0, uuids.calls)

    def test_firestore_reserved_authority_ids_fail_before_writes(self):
        cases = (
            ("reserved user", "__user__", "source-0001", "thread-1"),
            ("reserved source", "user-1", "__source__", "thread-1"),
            ("reserved thread", "user-1", "source-0001", "__thread__"),
        )
        for case, user_id, source_id, thread_id in cases:
            with self.subTest(case=case):
                fake = FakeFirestore()
                coordinator = self.module.SourceCoordinator(
                    fake,
                    uuid_factory=lambda: source_id,
                    now_factory=lambda: FROZEN_NOW,
                )
                with self.assertRaises(self.module.SourceCoordinatorConfigError):
                    coordinator.admit_or_repair_source_identity(
                        user_id=user_id,
                        hydrated_message={"id": "graph-A"},
                        evidence_kind="graph_hydration",
                        thread_id=thread_id,
                    )
                self.assertEqual({}, fake.data)
                self.assertEqual(
                    [],
                    [
                        event
                        for event in fake.events
                        if event[0] in {"create", "set", "update", "delete"}
                    ],
                )

        opaque_fake = FakeFirestore()
        opaque_coordinator = self.module.SourceCoordinator(
            opaque_fake,
            uuid_factory=lambda: "___",
            now_factory=lambda: FROZEN_NOW,
        )
        opaque_result = opaque_coordinator.admit_or_repair_source_identity(
            user_id="___",
            hydrated_message={"id": "graph-A"},
            evidence_kind="graph_hydration",
            thread_id="___",
        )
        self.assertEqual("___", opaque_result.canonical_source_id)

    def test_disjoint_sources_are_never_guessed_together(self):
        graph = self.admit(
            {"id": "graph-A", "conversationId": "same-conversation"}
        )
        rfc = self.admit(
            {
                "internetMessageId": "<rfc-B@example.test>",
                "conversationId": "same-conversation",
            },
            evidence_kind="operator_replay",
        )

        self.assertEqual("source-0001", graph.canonical_source_id)
        self.assertEqual("source-0002", rfc.canonical_source_id)
        self.assertNotEqual(graph.canonical_source_id, rfc.canonical_source_id)

    def test_no_raw_alias_or_proof_only_bridge_api_is_exposed(self):
        method = self.module.SourceCoordinator.admit_or_repair_source_identity
        parameters = tuple(inspect.signature(method).parameters)
        self.assertEqual(
            ("self", "user_id", "hydrated_message", "evidence_kind", "thread_id"),
            parameters,
        )
        for forbidden in (
            "aliases",
            "evidence_hash",
            "canonical_source_id",
            "repair_source_aliases",
            "bridge_source_aliases",
        ):
            self.assertFalse(hasattr(self.module.SourceCoordinator, forbidden))
        self.assertTrue(hasattr(self.module, "SourceAliasBridgeRequired"))
        self.assertEqual(
            "source_alias_bridge_required",
            self.module.SourceAliasBridgeRequired.code,
        )

        before = dict(self.fake.data)
        self.assertErrorCode(
            "source_identity_missing",
            lambda: self.admit(
                {"conversationId": "routing-proof-only"},
                evidence_kind="operator_replay",
            ),
        )
        self.assertEqual(before, self.fake.data)

    def test_ninth_retained_alias_fails_before_mutation(self):
        result = self.admit(
            {"id": "graph-A", "internetMessageId": "<rfc-1@example.test>"}
        )
        for number in range(2, 8):
            result = self.admit(
                {
                    "id": "graph-A",
                    "internetMessageId": f"<rfc-{number}@example.test>",
                }
            )
        self.assertEqual(8, self.module.MAX_SOURCE_ALIASES)
        identity = self.fake.data[self.identity_path(result.canonical_source_id)]
        self.assertEqual(8, len(identity["verifiedAliases"]))
        before_data = dict(self.fake.data)
        before_writes = list(self.write_events())

        self.assertErrorCode(
            "source_alias_limit_exceeded",
            lambda: self.admit(
                {
                    "id": "graph-A",
                    "internetMessageId": "<rfc-8@example.test>",
                }
            ),
        )

        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_identity_and_alias_schema_is_versioned_and_opaque(self):
        message = {
            "id": "graph-A",
            "internetMessageId": "<rfc-A@example.test>",
            "conversationId": "routing-only",
        }
        result = self.admit(message)
        identity = self.fake.data[self.identity_path(result.canonical_source_id)]
        expected_hash = self.module.canonical_json_hash(
            {
                "schemaVersion": 1,
                "evidenceKind": "graph_hydration",
                "hydratedMessage": message,
            }
        )
        self.assertEqual(
            {
                "schemaVersion",
                "canonicalSourceId",
                "creationHash",
                "verifiedAliases",
                "threadId",
                "lifecycleState",
                "createdAt",
                "updatedAt",
            },
            set(identity),
        )
        self.assertEqual("source-0001", identity["canonicalSourceId"])
        self.assertEqual(expected_hash, identity["creationHash"])
        self.assertEqual("pending", identity["lifecycleState"])
        self.assertEqual("thread-1", identity["threadId"])
        self.assertEqual(FROZEN_NOW, identity["createdAt"])
        self.assertEqual(FROZEN_NOW, identity["updatedAt"])
        self.assertEqual(
            sorted(
                identity["verifiedAliases"],
                key=lambda item: item["sourceAliasKey"],
            ),
            identity["verifiedAliases"],
        )
        aliases_by_key = {alias.key: alias for alias in result.aliases}
        for alias_path in self.alias_paths():
            projection = self.fake.data[alias_path]
            self.assertEqual(
                {
                    "schemaVersion",
                    "sourceAliasKey",
                    "aliasType",
                    "normalizedValueHash",
                    "canonicalSourceId",
                    "createdAt",
                },
                set(projection),
            )
            self.assertNotIn("graph-A", projection.values())
            self.assertNotIn("rfc-A@example.test", projection.values())
            alias = aliases_by_key[projection["sourceAliasKey"]]
            self.assertEqual(
                self.module.canonical_json_hash(
                    {
                        "schemaVersion": 1,
                        "hashKind": "source-alias-normalized-value-v1",
                        "aliasType": alias.alias_type,
                        "normalizedValue": alias.value,
                    }
                ),
                projection["normalizedValueHash"],
            )

    def test_bool_alias_projection_version_blocks_enrichment_without_writes(self):
        source = self.admit({"id": "graph-A"})
        alias_path = self.alias_paths()[0]
        self.fake.data[alias_path]["schemaVersion"] = True
        before_data = deepcopy(self.fake.data)
        before_writes = list(self.write_events())

        self.assertErrorCode(
            "source_alias_conflict",
            lambda: self.admit(
                {
                    "id": "graph-A",
                    "internetMessageId": "<rfc-A@example.test>",
                }
            ),
        )

        self.assertEqual("source-0001", source.canonical_source_id)
        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_hydrated_evidence_requires_exact_utf8_json(self):
        invalid_messages = (
            {"id": "graph-A", "nested": {1: "coerced-key"}},
            {"id": "graph-A", "nested": ("tuple",)},
            {"id": "graph-A", "nested": "\ud800"},
        )
        for message in invalid_messages:
            with self.subTest(message=repr(message)):
                before_data = deepcopy(self.fake.data)
                with self.assertRaises(
                    self.module.SourceCoordinatorConfigError
                ):
                    self.admit(message)
                self.assertEqual(before_data, self.fake.data)
        self.assertEqual(0, self.uuids.calls)

    def test_malformed_immutable_authority_schema_is_rejected(self):
        mutations = {
            "identity extra field": lambda identity, alias: identity.update(
                {"unexpectedOwner": "evil"}
            ),
            "alias extra owner": lambda identity, alias: alias.update(
                {"secondOwner": "evil"}
            ),
            "alias null timestamp": lambda identity, alias: alias.update(
                {"createdAt": None}
            ),
            "unsafe stored thread": lambda identity, alias: identity.update(
                {"threadId": "/"}
            ),
        }
        for case, mutate in mutations.items():
            with self.subTest(case=case):
                fake = FakeFirestore()
                coordinator = self.module.SourceCoordinator(
                    fake,
                    uuid_factory=SequentialUUIDs(),
                    now_factory=lambda: FROZEN_NOW,
                )
                result = coordinator.admit_or_repair_source_identity(
                    user_id="user-1",
                    hydrated_message={"id": "graph-A"},
                    evidence_kind="graph_hydration",
                    thread_id="thread-1",
                )
                identity_path = self.identity_path(result.canonical_source_id)
                alias_path = next(
                    path
                    for path in fake.data
                    if "/sourceAliases/" in path
                )
                mutate(fake.data[identity_path], fake.data[alias_path])
                before_data = deepcopy(fake.data)
                before_writes = [
                    event
                    for event in fake.events
                    if event[0] in {"create", "set", "update", "delete"}
                ]

                with self.assertRaises(
                    self.module.SourceCoordinatorAmbiguous
                ):
                    coordinator.admit_or_repair_source_identity(
                        user_id="user-1",
                        hydrated_message={"id": "graph-A"},
                        evidence_kind="graph_hydration",
                        thread_id="thread-1",
                    )

                self.assertEqual(before_data, fake.data)
                self.assertEqual(
                    before_writes,
                    [
                        event
                        for event in fake.events
                        if event[0] in {"create", "set", "update", "delete"}
                    ],
                )

    def test_stale_alias_enrichment_aborts_without_losing_monotonic_aliases(self):
        source = self.admit({"id": "graph-A"})
        first_envelope = self.module._build_source_admission_envelope(
            user_id="user-1",
            hydrated_message={
                "id": "graph-A",
                "internetMessageId": "<rfc-1@example.test>",
            },
            evidence_kind="graph_hydration",
        )
        second_envelope = self.module._build_source_admission_envelope(
            user_id="user-1",
            hydrated_message={
                "id": "graph-A",
                "internetMessageId": "<rfc-2@example.test>",
            },
            evidence_kind="graph_hydration",
        )
        first_transaction = self.fake.transaction(max_attempts=1)
        second_transaction = self.fake.transaction(max_attempts=1)
        first_transaction._begin()
        second_transaction._begin()
        self.coordinator._prepare_source_identity_transaction(
            transaction=first_transaction,
            user_id="user-1",
            envelope=first_envelope,
            validated_thread_id="thread-1",
        )
        self.coordinator._prepare_source_identity_transaction(
            transaction=second_transaction,
            user_id="user-1",
            envelope=second_envelope,
            validated_thread_id="thread-1",
        )

        first_transaction._commit()
        writes_after_first = len(self.write_events())
        with self.assertRaises(RuntimeError):
            second_transaction._commit()

        identity = self.fake.data[self.identity_path(source.canonical_source_id)]
        retained_keys = {
            descriptor["sourceAliasKey"]
            for descriptor in identity["verifiedAliases"]
        }
        first_keys = {alias.key for alias in first_envelope.aliases}
        second_only_key = next(
            alias.key
            for alias in second_envelope.aliases
            if alias.alias_type == "internet_message_id"
        )
        self.assertEqual(first_keys, retained_keys)
        self.assertNotIn(second_only_key, retained_keys)
        self.assertEqual(writes_after_first, len(self.write_events()))
        self.assertEqual(2, len(self.alias_paths()))


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_source_coordinator(self)
        required_types = (
            "ClassificationClaim",
            "ClassificationSnapshot",
        )
        required_methods = (
            "claim_source_classification",
            "record_classification_request_started",
            "persist_complete_classification_snapshot",
            "persist_deterministic_classification_snapshot",
            "require_authoritative_classification_snapshot",
            "classify_source_once",
        )
        for name in required_types:
            self.assertTrue(
                hasattr(self.module, name),
                f"{name} is absent",
            )
        self.assertTrue(
            hasattr(self.module, "SourceCoordinator"),
            "SourceCoordinator is absent",
        )
        for name in required_methods:
            self.assertTrue(
                hasattr(self.module.SourceCoordinator, name),
                f"SourceCoordinator.{name} is absent",
            )

        self.fake = FakeFirestore()
        self.clock = MutableClock(FROZEN_NOW)
        self.uuids = SequentialUUIDs()
        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
        )
        identity = self.coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={"id": "graph-A"},
            evidence_kind="graph_hydration",
            thread_id="thread-1",
        )
        self.source_id = identity.canonical_source_id
        self.fake.events.clear()

    @property
    def classification_path(self):
        return f"users/user-1/sourceClassifications/{self.source_id}"

    def classification_data(self):
        return self.fake.data[self.classification_path]

    def write_events(self):
        return [
            event
            for event in self.fake.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def assertErrorCode(self, expected_code, callable_):
        with self.assertRaises(self.module.SourceCoordinatorError) as raised:
            callable_()
        self.assertEqual(expected_code, raised.exception.code)
        return raised.exception

    def claim(self, *, lease_seconds=60):
        return self.coordinator.claim_source_classification(
            user_id="user-1",
            canonical_source_id=self.source_id,
            lease_seconds=lease_seconds,
        )

    def start(self, claim, *, classification_input=CLASSIFICATION_INPUT):
        return self.coordinator.record_classification_request_started(
            user_id="user-1",
            canonical_source_id=self.source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            model_request_key="model-request-1",
            classification_input=classification_input,
        )

    def persist(self, claim, *, complete_proposal=COMPLETE_PROPOSAL):
        return self.coordinator.persist_complete_classification_snapshot(
            user_id="user-1",
            canonical_source_id=self.source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            complete_proposal=complete_proposal,
            proposal_evidence=MODEL_PROPOSAL_EVIDENCE,
        )

    def classify(self, classifier, *, classification_input=CLASSIFICATION_INPUT):
        return self.coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=self.source_id,
            lease_seconds=60,
            classification_input=classification_input,
            classifier=classifier,
        )

    def test_first_claim_is_epoch_one_unique_and_does_not_start_model(self):
        claim = self.claim()

        self.assertIsInstance(claim, self.module.ClassificationClaim)
        self.assertEqual(self.source_id, claim.canonical_source_id)
        self.assertEqual(1, claim.classification_epoch)
        self.assertNotEqual(self.source_id, claim.classification_claim_id)
        self.assertGreater(claim.lease_expires_at, self.clock.current)
        with self.assertRaises(FrozenInstanceError):
            claim.classification_epoch = 2
        stored = self.classification_data()
        self.assertEqual("claimed", stored["classificationState"])
        self.assertEqual("not_started", stored["modelRequestState"])
        self.assertIsNone(stored["classificationInputHash"])
        self.assertIsNone(stored["modelRequestKey"])

    def test_bool_identity_version_blocks_claim_without_writes(self):
        identity_path = f"users/user-1/sourceIdentities/{self.source_id}"
        self.fake.data[identity_path]["schemaVersion"] = True
        before_data = deepcopy(self.fake.data)
        before_writes = list(self.write_events())

        self.assertErrorCode(
            "source_coordinator_ambiguous",
            self.claim,
        )

        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_bool_classification_versions_block_request_start_without_writes(self):
        for field_name in ("schemaVersion", "classificationInputSchemaVersion"):
            with self.subTest(field_name=field_name):
                fake = FakeFirestore()
                clock = MutableClock(FROZEN_NOW)
                coordinator = self.module.SourceCoordinator(
                    fake,
                    uuid_factory=SequentialUUIDs(),
                    now_factory=clock,
                )
                source = coordinator.admit_or_repair_source_identity(
                    user_id="user-1",
                    hydrated_message={"id": "graph-A"},
                    evidence_kind="graph_hydration",
                    thread_id="thread-1",
                )
                claim = coordinator.claim_source_classification(
                    user_id="user-1",
                    canonical_source_id=source.canonical_source_id,
                    lease_seconds=60,
                )
                classification_path = (
                    "users/user-1/sourceClassifications/"
                    f"{source.canonical_source_id}"
                )
                fake.data[classification_path][field_name] = True
                before_data = deepcopy(fake.data)
                before_writes = [
                    event
                    for event in fake.events
                    if event[0] in {"create", "set", "update", "delete"}
                ]

                self.assertErrorCode(
                    "source_coordinator_ambiguous",
                    lambda: coordinator.record_classification_request_started(
                        user_id="user-1",
                        canonical_source_id=source.canonical_source_id,
                        classification_epoch=claim.classification_epoch,
                        classification_claim_id=claim.classification_claim_id,
                        model_request_key="model-request-1",
                        classification_input=CLASSIFICATION_INPUT,
                    ),
                )

                self.assertEqual(before_data, fake.data)
                self.assertEqual(
                    before_writes,
                    [
                        event
                        for event in fake.events
                        if event[0] in {"create", "set", "update", "delete"}
                    ],
                )

    def test_bool_classification_versions_block_snapshot_require_and_retry(self):
        for field_name in ("schemaVersion", "classificationInputSchemaVersion"):
            with self.subTest(field_name=field_name):
                fake = FakeFirestore()
                clock = MutableClock(FROZEN_NOW)
                uuids = SequentialUUIDs()
                coordinator = self.module.SourceCoordinator(
                    fake,
                    uuid_factory=uuids,
                    now_factory=clock,
                )
                source = coordinator.admit_or_repair_source_identity(
                    user_id="user-1",
                    hydrated_message={"id": "graph-A"},
                    evidence_kind="graph_hydration",
                    thread_id="thread-1",
                )
                snapshot = coordinator.classify_source_once(
                    user_id="user-1",
                    canonical_source_id=source.canonical_source_id,
                    lease_seconds=60,
                    classification_input=CLASSIFICATION_INPUT,
                    classifier=lambda: (
                        deepcopy(COMPLETE_PROPOSAL),
                        deepcopy(MODEL_PROPOSAL_EVIDENCE),
                    ),
                )
                classification_path = (
                    "users/user-1/sourceClassifications/"
                    f"{source.canonical_source_id}"
                )
                fake.data[classification_path][field_name] = True
                before_data = deepcopy(fake.data)
                before_writes = [
                    event
                    for event in fake.events
                    if event[0] in {"create", "set", "update", "delete"}
                ]
                callback_calls = 0

                def classifier():
                    nonlocal callback_calls
                    callback_calls += 1
                    raise AssertionError("malformed snapshot called classifier")

                self.assertIsInstance(snapshot, self.module.ClassificationSnapshot)
                self.assertErrorCode(
                    "source_coordinator_ambiguous",
                    lambda: coordinator.require_authoritative_classification_snapshot(
                        user_id="user-1",
                        canonical_source_id=source.canonical_source_id,
                    ),
                )
                self.assertErrorCode(
                    "source_coordinator_ambiguous",
                    lambda: coordinator.classify_source_once(
                        user_id="user-1",
                        canonical_source_id=source.canonical_source_id,
                        lease_seconds=60,
                        classification_input=CLASSIFICATION_INPUT,
                        classifier=classifier,
                    ),
                )

                self.assertEqual(0, callback_calls)
                self.assertEqual(before_data, fake.data)
                self.assertEqual(
                    before_writes,
                    [
                        event
                        for event in fake.events
                        if event[0] in {"create", "set", "update", "delete"}
                    ],
                )

    def test_expired_claim_before_request_start_gets_higher_epoch(self):
        first = self.claim()
        self.clock.advance(seconds=61)
        second = self.claim()

        self.assertEqual(2, second.classification_epoch)
        self.assertNotEqual(
            first.classification_claim_id,
            second.classification_claim_id,
        )
        started = self.start(second)
        self.assertTrue(started.newly_started)
        self.assertErrorCode(
            "classification_claim_conflict",
            lambda: self.start(first),
        )

    def test_request_intent_is_committed_before_classifier_callback(self):
        observations = []

        def classifier():
            stored = deepcopy(self.classification_data())
            observations.append(stored)
            return deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE)

        snapshot = self.classify(classifier)

        self.assertEqual(1, len(observations))
        self.assertEqual("request_started", observations[0]["classificationState"])
        self.assertEqual("started", observations[0]["modelRequestState"])
        self.assertEqual(
            self.module.canonical_json_hash(CLASSIFICATION_INPUT),
            observations[0]["classificationInputHash"],
        )
        self.assertTrue(observations[0]["modelRequestKey"])
        self.assertEqual(
            self.module.canonical_json_hash(COMPLETE_PROPOSAL),
            snapshot.complete_proposal_hash,
        )

    def test_request_start_exact_replay_is_not_fresh_and_input_drift_blocks(self):
        claim = self.claim()
        first = self.start(claim)
        replay = self.start(claim)
        writes_before_drift = list(self.write_events())

        self.assertTrue(first.newly_started)
        self.assertFalse(replay.newly_started)
        self.assertErrorCode(
            "classification_input_conflict",
            lambda: self.start(
                claim,
                classification_input={**CLASSIFICATION_INPUT, "drift": True},
            ),
        )
        self.assertEqual(writes_before_drift, self.write_events())

    def test_concurrent_identical_start_stale_loser_is_not_authorized(self):
        claim = self.claim()
        winner_results = []

        self.fake.before_next_commit_hook = lambda: winner_results.append(
            self.start(claim)
        )

        self.assertErrorCode(
            "classification_request_ambiguous",
            lambda: self.start(claim),
        )
        self.assertEqual(1, len(winner_results))
        self.assertTrue(winner_results[0].newly_started)
        self.assertEqual("request_started", self.classification_data()["classificationState"])
        self.assertEqual(
            1,
            sum(event[0] == "update" for event in self.write_events()),
        )

    def test_expired_claim_cannot_start_but_owned_started_request_can_capture(self):
        expired_claim = self.claim()
        self.clock.advance(seconds=61)
        self.assertErrorCode(
            "classification_claim_expired",
            lambda: self.start(expired_claim),
        )

        takeover = self.claim()
        self.start(takeover)
        self.clock.advance(seconds=61)
        snapshot = self.persist(takeover)
        self.assertEqual(self.source_id, snapshot.canonical_source_id)

    def test_expired_started_request_authorizes_zero_second_callbacks(self):
        claim = self.claim()
        self.start(claim)
        self.clock.advance(seconds=61)
        calls = 0

        def classifier():
            nonlocal calls
            calls += 1
            return deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE)

        self.assertErrorCode(
            "classification_request_ambiguous",
            lambda: self.classify(classifier),
        )
        self.assertEqual(0, calls)
        self.assertEqual(
            "classification_request_ambiguous",
            self.classification_data()["classificationState"],
        )
        self.assertEqual("ambiguous", self.classification_data()["modelRequestState"])

    def test_expired_started_request_drift_cannot_suppress_ambiguity_fence(self):
        claim = self.claim()
        self.start(claim)
        self.clock.advance(seconds=61)
        writes_before_recovery = list(self.write_events())
        calls = 0

        def classifier():
            nonlocal calls
            calls += 1
            raise AssertionError("expired drift recovery called classifier")

        self.assertErrorCode(
            "classification_request_ambiguous",
            lambda: self.classify(
                classifier,
                classification_input={**CLASSIFICATION_INPUT, "drift": True},
            ),
        )

        self.assertEqual(0, calls)
        stored = self.classification_data()
        self.assertEqual(
            "classification_request_ambiguous",
            stored["classificationState"],
        )
        self.assertEqual("ambiguous", stored["modelRequestState"])
        recovery_writes = self.write_events()[len(writes_before_recovery) :]
        self.assertEqual(1, len(recovery_writes))
        self.assertEqual("update", recovery_writes[0][0])
        self.assertEqual(self.classification_path, recovery_writes[0][1])

    def test_active_started_request_drift_remains_input_conflict(self):
        claim = self.claim()
        self.start(claim)
        writes_before_retry = list(self.write_events())
        calls = 0

        def classifier():
            nonlocal calls
            calls += 1
            raise AssertionError("active drift retry called classifier")

        self.assertErrorCode(
            "classification_input_conflict",
            lambda: self.classify(
                classifier,
                classification_input={**CLASSIFICATION_INPUT, "drift": True},
            ),
        )

        self.assertEqual(0, calls)
        self.assertEqual(
            "request_started",
            self.classification_data()["classificationState"],
        )
        self.assertEqual(writes_before_retry, self.write_events())

    def test_expired_started_request_drift_apply_then_raise_uses_exact_readback(self):
        claim = self.claim()
        self.start(claim)
        self.clock.advance(seconds=61)
        writes_before_recovery = list(self.write_events())
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown commit")
        calls = 0

        def classifier():
            nonlocal calls
            calls += 1
            raise AssertionError("expired request recovery called classifier")

        self.assertErrorCode(
            "classification_request_ambiguous",
            lambda: self.classify(
                classifier,
                classification_input={**CLASSIFICATION_INPUT, "drift": True},
            ),
        )

        self.assertEqual(0, calls)
        stored = self.classification_data()
        self.assertEqual(
            "classification_request_ambiguous",
            stored["classificationState"],
        )
        self.assertEqual("ambiguous", stored["modelRequestState"])
        self.assertEqual(
            1,
            sum(event[0] == "commit_raised_after_apply" for event in self.fake.events),
        )
        recovery_writes = self.write_events()[len(writes_before_recovery) :]
        self.assertEqual(1, len(recovery_writes))
        self.assertEqual("update", recovery_writes[0][0])
        self.assertEqual(self.classification_path, recovery_writes[0][1])

    def test_snapshot_apply_then_raise_is_accepted_by_exact_readback(self):
        claim = self.claim()
        self.start(claim)
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown commit")

        snapshot = self.persist(claim)

        self.assertIsInstance(snapshot, self.module.ClassificationSnapshot)
        self.assertEqual(self.source_id, snapshot.canonical_source_id)
        self.assertEqual(COMPLETE_PROPOSAL, snapshot.complete_proposal)
        self.assertEqual("snapshot_ready", self.classification_data()["classificationState"])
        self.assertEqual("captured", self.classification_data()["modelRequestState"])

    def test_different_snapshot_retry_conflicts_without_writes(self):
        claim = self.claim()
        self.start(claim)
        original = self.persist(claim)
        before_data = deepcopy(self.fake.data)
        before_writes = list(self.write_events())

        different = deepcopy(COMPLETE_PROPOSAL)
        different["ordinaryObligations"].append(
            {"type": "informational", "message": "new"}
        )
        self.assertErrorCode(
            "classification_snapshot_conflict",
            lambda: self.persist(claim, complete_proposal=different),
        )
        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())
        self.assertEqual(
            original,
            self.coordinator.require_authoritative_classification_snapshot(
                user_id="user-1",
                canonical_source_id=self.source_id,
            ),
        )

    def test_oversize_snapshot_fails_before_mutation(self):
        claim = self.claim()
        self.start(claim)
        before_data = deepcopy(self.fake.data)
        before_writes = list(self.write_events())
        oversized = {
            "schemaVersion": 1,
            "transitionCandidates": [],
            "ordinaryObligations": [
                {"type": "informational", "payload": "x" * 614401}
            ],
        }

        self.assertErrorCode(
            "classification_snapshot_too_large",
            lambda: self.persist(claim, complete_proposal=oversized),
        )
        self.assertEqual(before_data, self.fake.data)
        self.assertEqual(before_writes, self.write_events())

    def test_complete_proposal_requires_exact_versioned_shape_and_legal_lanes(self):
        invalid_proposals = {
            "empty": {},
            "missing transition list": {
                "schemaVersion": 1,
                "ordinaryObligations": [],
            },
            "missing ordinary list": {
                "schemaVersion": 1,
                "transitionCandidates": [],
            },
            "wrong schema": {
                "schemaVersion": 2,
                "transitionCandidates": [],
                "ordinaryObligations": [],
            },
            "bool schema": {
                "schemaVersion": True,
                "transitionCandidates": [],
                "ordinaryObligations": [],
            },
            "extra top-level field": {
                "schemaVersion": 1,
                "transitionCandidates": [],
                "ordinaryObligations": [],
                "winner": "none",
            },
            "ordinary item in transition lane": {
                "schemaVersion": 1,
                "transitionCandidates": [{"type": "field_update"}],
                "ordinaryObligations": [],
            },
            "human item in ordinary lane": {
                "schemaVersion": 1,
                "transitionCandidates": [],
                "ordinaryObligations": [{"type": "call_requested"}],
            },
            "hard item in ordinary lane": {
                "schemaVersion": 1,
                "transitionCandidates": [],
                "ordinaryObligations": [{"type": "contact_optout"}],
            },
            "unknown ordinary item": {
                "schemaVersion": 1,
                "transitionCandidates": [],
                "ordinaryObligations": [{"type": "unknown_work"}],
            },
        }

        for case, proposal in invalid_proposals.items():
            with self.subTest(case=case):
                fake = FakeFirestore()
                clock = MutableClock(FROZEN_NOW)
                uuids = SequentialUUIDs()
                coordinator = self.module.SourceCoordinator(
                    fake,
                    uuid_factory=uuids,
                    now_factory=clock,
                )
                source = coordinator.admit_or_repair_source_identity(
                    user_id="user-1",
                    hydrated_message={"id": "graph-A"},
                    evidence_kind="graph_hydration",
                    thread_id="thread-1",
                )
                claim = coordinator.claim_source_classification(
                    user_id="user-1",
                    canonical_source_id=source.canonical_source_id,
                    lease_seconds=60,
                )
                coordinator.record_classification_request_started(
                    user_id="user-1",
                    canonical_source_id=source.canonical_source_id,
                    classification_epoch=claim.classification_epoch,
                    classification_claim_id=claim.classification_claim_id,
                    model_request_key="model-request-1",
                    classification_input=CLASSIFICATION_INPUT,
                )
                before_data = deepcopy(fake.data)
                before_writes = [
                    event
                    for event in fake.events
                    if event[0] in {"create", "set", "update", "delete"}
                ]

                with self.assertRaises(self.module.SourceCoordinatorConfigError):
                    coordinator.persist_complete_classification_snapshot(
                        user_id="user-1",
                        canonical_source_id=source.canonical_source_id,
                        classification_epoch=claim.classification_epoch,
                        classification_claim_id=claim.classification_claim_id,
                        complete_proposal=proposal,
                        proposal_evidence=MODEL_PROPOSAL_EVIDENCE,
                    )

                self.assertEqual(before_data, fake.data)
                self.assertEqual(
                    before_writes,
                    [
                        event
                        for event in fake.events
                        if event[0] in {"create", "set", "update", "delete"}
                    ],
                )

    def test_snapshot_ready_recovery_calls_classifier_zero_times(self):
        first = self.classify(
            lambda: (deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE))
        )
        calls = 0

        def classifier():
            nonlocal calls
            calls += 1
            raise AssertionError("snapshot recovery called classifier")

        second = self.classify(classifier)

        self.assertEqual(0, calls)
        self.assertEqual(first, second)

    def test_snapshot_ready_recovery_rejects_input_drift_without_callback(self):
        self.classify(
            lambda: (deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE))
        )
        calls = 0

        def classifier():
            nonlocal calls
            calls += 1
            return deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE)

        self.assertErrorCode(
            "classification_input_conflict",
            lambda: self.classify(
                classifier,
                classification_input={**CLASSIFICATION_INPUT, "drift": True},
            ),
        )
        self.assertEqual(0, calls)

    def test_snapshot_result_and_persisted_payload_are_detached_and_immutable(self):
        proposal = deepcopy(COMPLETE_PROPOSAL)
        evidence = deepcopy(MODEL_PROPOSAL_EVIDENCE)
        snapshot = self.classify(lambda: (proposal, evidence))

        proposal["schemaVersion"] = 99
        evidence["schemaVersion"] = 99
        self.assertEqual(1, self.classification_data()["completeProposalSnapshot"]["schemaVersion"])
        self.assertEqual(1, self.classification_data()["proposalEvidence"]["schemaVersion"])
        self.assertNotIsInstance(snapshot.complete_proposal, dict)
        with self.assertRaises(TypeError):
            snapshot.complete_proposal["schemaVersion"] = 99
        with self.assertRaises(TypeError):
            dict.__setitem__(snapshot.complete_proposal, "schemaVersion", 99)

    def test_classifier_failure_durably_marks_owned_request_ambiguous(self):
        def classifier():
            raise RuntimeError("model transport failed after request start")

        self.assertErrorCode(
            "classification_request_ambiguous",
            lambda: self.classify(classifier),
        )
        stored = self.classification_data()
        self.assertEqual(
            "classification_request_ambiguous",
            stored["classificationState"],
        )
        self.assertEqual("ambiguous", stored["modelRequestState"])

        second_calls = 0

        def second_classifier():
            nonlocal second_calls
            second_calls += 1
            return deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE)

        self.assertErrorCode(
            "classification_request_ambiguous",
            lambda: self.classify(second_classifier),
        )
        self.assertEqual(0, second_calls)

    def test_malformed_classifier_capture_is_fenced_as_ambiguous(self):
        malformed = {
            "schemaVersion": 1,
            "transitionCandidates": [{"type": "unknown_transition_shape"}],
            "ordinaryObligations": [],
        }

        self.assertErrorCode(
            "classification_request_ambiguous",
            lambda: self.classify(
                lambda: (malformed, deepcopy(MODEL_PROPOSAL_EVIDENCE))
            ),
        )
        self.assertEqual(
            "classification_request_ambiguous",
            self.classification_data()["classificationState"],
        )

    def test_snapshot_apply_then_raise_requires_identity_readback(self):
        fake = CorruptingReadbackFirestore()
        clock = MutableClock(FROZEN_NOW)
        uuids = SequentialUUIDs()
        coordinator = self.module.SourceCoordinator(
            fake,
            uuid_factory=uuids,
            now_factory=clock,
        )
        source = coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={"id": "graph-A"},
            evidence_kind="graph_hydration",
            thread_id="thread-1",
        )
        claim = coordinator.claim_source_classification(
            user_id="user-1",
            canonical_source_id=source.canonical_source_id,
            lease_seconds=60,
        )
        coordinator.record_classification_request_started(
            user_id="user-1",
            canonical_source_id=source.canonical_source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            model_request_key="model-request-1",
            classification_input=CLASSIFICATION_INPUT,
        )
        fake.hidden_readback_path = (
            f"users/user-1/sourceIdentities/{source.canonical_source_id}"
        )
        fake.apply_then_raise_next_commit = RuntimeError("unknown commit")

        with self.assertRaises(self.module.SourceCoordinatorAmbiguous):
            coordinator.persist_complete_classification_snapshot(
                user_id="user-1",
                canonical_source_id=source.canonical_source_id,
                classification_epoch=claim.classification_epoch,
                classification_claim_id=claim.classification_claim_id,
                complete_proposal=COMPLETE_PROPOSAL,
                proposal_evidence=MODEL_PROPOSAL_EVIDENCE,
            )

    def test_verified_deterministic_hard_optout_skips_model_and_freezes(self):
        verifier_calls = 0

        def verifier(classification_input):
            nonlocal verifier_calls
            verifier_calls += 1
            self.assertEqual(CLASSIFICATION_INPUT, classification_input)
            return deepcopy(HARD_OPTOUT_EVIDENCE)

        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
            hard_optout_verifier=verifier,
        )
        classifier_calls = 0

        def classifier():
            nonlocal classifier_calls
            classifier_calls += 1
            raise AssertionError("verified hard opt-out called model")

        try:
            snapshot = self.classify(classifier)
        except self.module.SourceCoordinatorConfigError as exc:
            self.fail(f"exact hard opt-out evidence dict was rejected: {exc}")

        self.assertEqual(1, verifier_calls)
        self.assertEqual(0, classifier_calls)
        stored = self.classification_data()
        self.assertEqual("snapshot_ready", stored["classificationState"])
        self.assertEqual("not_applicable", stored["modelRequestState"])
        self.assertIsNone(stored["modelRequestKey"])
        self.assertEqual(
            self.module.canonical_json_hash(CLASSIFICATION_INPUT),
            stored["classificationInputHash"],
        )
        self.assertEqual(
            "contact_optout",
            snapshot.complete_proposal["transitionCandidates"][0]["type"],
        )

    def test_verified_evidence_has_no_external_mint_or_capability(self):
        self.assertFalse(hasattr(self.module, "_mint_verified_hard_optout_evidence"))
        self.assertFalse(
            hasattr(self.module, "_VERIFIED_HARD_OPTOUT_EVIDENCE_CAPABILITY")
        )

    def test_verifier_rejects_private_objects_and_non_exact_evidence_dicts(self):
        forged_private = object.__new__(self.module._VerifiedHardOptoutEvidence)
        object.__setattr__(forged_private, "evidence", HARD_OPTOUT_EVIDENCE)

        class EvidenceDict(dict):
            pass

        invalid_results = {
            "private object": forged_private,
            "mapping subclass": EvidenceDict(HARD_OPTOUT_EVIDENCE),
            "key subclass": {
                HostileString(key): value
                for key, value in HARD_OPTOUT_EVIDENCE.items()
            },
            "extra field": {**HARD_OPTOUT_EVIDENCE, "verified": True},
            "bool schema": {**HARD_OPTOUT_EVIDENCE, "schemaVersion": True},
            "wrong schema": {**HARD_OPTOUT_EVIDENCE, "schemaVersion": 2},
            "empty evidence kind": {**HARD_OPTOUT_EVIDENCE, "evidenceKind": ""},
            "kind subclass": {
                **HARD_OPTOUT_EVIDENCE,
                "evidenceKind": HostileString("header_list_unsubscribe"),
            },
            "short hash": {**HARD_OPTOUT_EVIDENCE, "evidenceHash": "b" * 63},
            "uppercase hash": {**HARD_OPTOUT_EVIDENCE, "evidenceHash": "B" * 64},
        }
        for case, verifier_result in invalid_results.items():
            with self.subTest(case=case):
                fake = FakeFirestore()
                clock = MutableClock(FROZEN_NOW)
                coordinator = self.module.SourceCoordinator(
                    fake,
                    uuid_factory=SequentialUUIDs(),
                    now_factory=clock,
                    hard_optout_verifier=lambda _: verifier_result,
                )
                source = coordinator.admit_or_repair_source_identity(
                    user_id="user-1",
                    hydrated_message={"id": "graph-A"},
                    evidence_kind="graph_hydration",
                    thread_id="thread-1",
                )
                claim = coordinator.claim_source_classification(
                    user_id="user-1",
                    canonical_source_id=source.canonical_source_id,
                    lease_seconds=60,
                )
                before_data = deepcopy(fake.data)
                before_writes = [
                    event
                    for event in fake.events
                    if event[0] in {"create", "set", "update", "delete"}
                ]

                with self.assertRaises(self.module.SourceCoordinatorConfigError):
                    coordinator.persist_deterministic_classification_snapshot(
                        user_id="user-1",
                        canonical_source_id=source.canonical_source_id,
                        classification_epoch=claim.classification_epoch,
                        classification_claim_id=claim.classification_claim_id,
                        classification_input=CLASSIFICATION_INPUT,
                    )

                self.assertEqual(before_data, fake.data)
                self.assertEqual(
                    before_writes,
                    [
                        event
                        for event in fake.events
                        if event[0] in {"create", "set", "update", "delete"}
                    ],
                )

    def test_verifier_validates_key_types_before_private_construction(self):
        hostile_keys = {
            HostileString(key): value
            for key, value in HARD_OPTOUT_EVIDENCE.items()
        }
        construction_calls = 0
        original_private_type = self.module._VerifiedHardOptoutEvidence

        class ConstructionProbe:
            def __init__(probe_self, *, evidence):
                nonlocal construction_calls
                construction_calls += 1
                probe_self.evidence = evidence

        self.module._VerifiedHardOptoutEvidence = ConstructionProbe
        try:
            self.coordinator = self.module.SourceCoordinator(
                self.fake,
                uuid_factory=self.uuids,
                now_factory=self.clock,
                hard_optout_verifier=lambda _: hostile_keys,
            )
            claim = self.claim()
            self.assertErrorCode(
                "source_coordinator_config",
                lambda: self.coordinator.persist_deterministic_classification_snapshot(
                    user_id="user-1",
                    canonical_source_id=self.source_id,
                    classification_epoch=claim.classification_epoch,
                    classification_claim_id=claim.classification_claim_id,
                    classification_input=CLASSIFICATION_INPUT,
                ),
            )
        finally:
            self.module._VerifiedHardOptoutEvidence = original_private_type

        self.assertEqual(0, construction_calls)

    def test_deterministic_no_match_validates_authority_inside_one_transaction(self):
        observations = []

        def verifier(classification_input):
            observations.append(tuple(self.fake.events))
            return None

        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
            hard_optout_verifier=verifier,
        )
        claim = self.claim()
        self.fake.events.clear()

        result = self.coordinator.persist_deterministic_classification_snapshot(
            user_id="user-1",
            canonical_source_id=self.source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            classification_input=CLASSIFICATION_INPUT,
        )

        self.assertIsNone(result)
        self.assertEqual(1, len(observations))
        observed_events = observations[0]
        self.assertEqual(1, sum(event[0] == "transaction_began" for event in observed_events))
        self.assertEqual(
            [
                f"users/user-1/sourceIdentities/{self.source_id}",
                self.classification_path,
            ],
            [event[1] for event in observed_events if event[0] == "get"],
        )
        self.assertEqual([], self.write_events())
        self.assertEqual("claimed", self.classification_data()["classificationState"])

    def test_invalid_deterministic_claims_never_invoke_verifier(self):
        verifier_calls = 0

        def verifier(classification_input):
            nonlocal verifier_calls
            verifier_calls += 1
            return deepcopy(HARD_OPTOUT_EVIDENCE)

        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
            hard_optout_verifier=verifier,
        )
        expired = self.claim()
        self.clock.advance(seconds=61)
        self.assertErrorCode(
            "classification_claim_expired",
            lambda: self.coordinator.persist_deterministic_classification_snapshot(
                user_id="user-1",
                canonical_source_id=self.source_id,
                classification_epoch=expired.classification_epoch,
                classification_claim_id=expired.classification_claim_id,
                classification_input=CLASSIFICATION_INPUT,
            ),
        )
        self.assertEqual(0, verifier_calls)

        current = self.claim()
        self.assertErrorCode(
            "classification_claim_conflict",
            lambda: self.coordinator.persist_deterministic_classification_snapshot(
                user_id="user-1",
                canonical_source_id=self.source_id,
                classification_epoch=expired.classification_epoch,
                classification_claim_id=expired.classification_claim_id,
                classification_input=CLASSIFICATION_INPUT,
            ),
        )
        self.assertEqual(0, verifier_calls)
        self.assertEqual(2, current.classification_epoch)

        missing_identity_fake = FakeFirestore()
        missing_identity_coordinator = self.module.SourceCoordinator(
            missing_identity_fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
            hard_optout_verifier=verifier,
        )
        self.assertErrorCode(
            "source_identity_missing",
            lambda: missing_identity_coordinator.persist_deterministic_classification_snapshot(
                user_id="user-1",
                canonical_source_id="missing-source",
                classification_epoch=1,
                classification_claim_id="missing-claim",
                classification_input=CLASSIFICATION_INPUT,
            ),
        )
        self.assertEqual(0, verifier_calls)

    def test_deterministic_snapshot_retry_verifies_exact_evidence_after_input_gate(self):
        verifier_calls = 0

        def verifier(classification_input):
            nonlocal verifier_calls
            verifier_calls += 1
            return deepcopy(HARD_OPTOUT_EVIDENCE)

        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
            hard_optout_verifier=verifier,
        )
        claim = self.claim()
        first = self.coordinator.persist_deterministic_classification_snapshot(
            user_id="user-1",
            canonical_source_id=self.source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            classification_input=CLASSIFICATION_INPUT,
        )
        writes_after_first = list(self.write_events())

        exact_retry = self.coordinator.persist_deterministic_classification_snapshot(
            user_id="user-1",
            canonical_source_id=self.source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            classification_input=CLASSIFICATION_INPUT,
        )

        self.assertEqual(first, exact_retry)
        self.assertEqual(2, verifier_calls)
        self.assertEqual(writes_after_first, self.write_events())

        self.assertErrorCode(
            "classification_snapshot_conflict",
            lambda: self.coordinator.persist_deterministic_classification_snapshot(
                user_id="user-1",
                canonical_source_id=self.source_id,
                classification_epoch=claim.classification_epoch,
                classification_claim_id=claim.classification_claim_id,
                classification_input={**CLASSIFICATION_INPUT, "drift": True},
            ),
        )
        self.assertEqual(2, verifier_calls)
        self.assertEqual(writes_after_first, self.write_events())

    def test_expired_deterministic_snapshot_retry_still_verifies_evidence(self):
        evidence_hash = "b" * 64
        verifier_calls = 0

        def verifier(classification_input):
            nonlocal verifier_calls
            verifier_calls += 1
            return {
                **HARD_OPTOUT_EVIDENCE,
                "evidenceHash": evidence_hash,
            }

        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
            hard_optout_verifier=verifier,
        )
        claim = self.claim()
        self.coordinator.persist_deterministic_classification_snapshot(
            user_id="user-1",
            canonical_source_id=self.source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            classification_input=CLASSIFICATION_INPUT,
        )
        self.clock.advance(seconds=61)
        evidence_hash = "c" * 64
        before_writes = list(self.write_events())

        self.assertErrorCode(
            "classification_snapshot_conflict",
            lambda: self.coordinator.persist_deterministic_classification_snapshot(
                user_id="user-1",
                canonical_source_id=self.source_id,
                classification_epoch=claim.classification_epoch,
                classification_claim_id=claim.classification_claim_id,
                classification_input=CLASSIFICATION_INPUT,
            ),
        )
        self.assertEqual(2, verifier_calls)
        self.assertEqual(before_writes, self.write_events())

        coordinator_without_verifier = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
        )
        self.assertErrorCode(
            "classification_snapshot_conflict",
            lambda: coordinator_without_verifier.persist_deterministic_classification_snapshot(
                user_id="user-1",
                canonical_source_id=self.source_id,
                classification_epoch=claim.classification_epoch,
                classification_claim_id=claim.classification_claim_id,
                classification_input=CLASSIFICATION_INPUT,
            ),
        )

    def test_model_evidence_cannot_assert_deterministic_hard_optout(self):
        model_calls = 0
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "contact_optout", "verified": True}
            ],
            "ordinaryObligations": [],
        }
        evidence = {
            "schemaVersion": 1,
            "evidenceKind": "model_claimed_hard_optout",
            "verified": True,
        }

        def classifier():
            nonlocal model_calls
            model_calls += 1
            return proposal, evidence

        self.classify(classifier)

        self.assertEqual(1, model_calls)
        stored = self.classification_data()
        self.assertEqual("captured", stored["modelRequestState"])
        self.assertIsNone(stored["deterministicEvidence"])
        self.assertEqual(
            [
                {
                    "type": "needs_user_input",
                    "reason": "unverified_optout_review",
                    "sourceCandidateHash": self.module.canonical_json_hash(
                        proposal["transitionCandidates"][0]
                    ),
                }
            ],
            stored["transitionCandidates"],
        )
        self.assertEqual(
            "human_decision",
            stored["selectionSnapshot"]["ownerKind"],
        )
        self.assertNotIn("contact_optout", json.dumps(stored["selectionSnapshot"]))

    def test_semantic_work_permutations_freeze_identical_hashes(self):
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "call_requested", "phone": "555-0100"},
                {"type": "property_unavailable", "property": "A"},
            ],
            "ordinaryObligations": [
                {"type": "field_update", "field": "stage", "value": "warm"},
                {"type": "informational", "message": "retained"},
            ],
        }

        def freeze(candidate):
            fake = FakeFirestore()
            clock = MutableClock(FROZEN_NOW)
            uuids = SequentialUUIDs()
            coordinator = self.module.SourceCoordinator(
                fake,
                uuid_factory=uuids,
                now_factory=clock,
            )
            source = coordinator.admit_or_repair_source_identity(
                user_id="user-1",
                hydrated_message={"id": "graph-A"},
                evidence_kind="graph_hydration",
                thread_id="thread-1",
            )
            snapshot = coordinator.classify_source_once(
                user_id="user-1",
                canonical_source_id=source.canonical_source_id,
                lease_seconds=60,
                classification_input=CLASSIFICATION_INPUT,
                classifier=lambda: (
                    deepcopy(candidate),
                    deepcopy(MODEL_PROPOSAL_EVIDENCE),
                ),
            )
            return snapshot

        permuted = deepcopy(proposal)
        permuted["transitionCandidates"].reverse()
        permuted["ordinaryObligations"].reverse()
        first = freeze(proposal)
        second = freeze(permuted)

        self.assertEqual(first.complete_proposal_hash, second.complete_proposal_hash)
        self.assertEqual(first.selection_hash, second.selection_hash)
        self.assertEqual(first.snapshot_immutable_hash, second.snapshot_immutable_hash)

    def test_snapshot_commit_writes_only_classification_authority(self):
        self.classify(
            lambda: (deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE))
        )

        self.assertTrue(self.write_events())
        for event in self.write_events():
            self.assertEqual(self.classification_path, event[1])
            self.assertNotRegex(
                event[1],
                r"owner|ledger|marker|thread|queue|draft|reply|provider",
            )

    def test_two_workers_call_classifier_once(self):
        outer_calls = 0
        inner_calls = 0
        inner_error = None

        def inner_classifier():
            nonlocal inner_calls
            inner_calls += 1
            return deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE)

        def outer_classifier():
            nonlocal outer_calls, inner_error
            outer_calls += 1
            try:
                self.classify(inner_classifier)
            except self.module.SourceCoordinatorError as exc:
                inner_error = exc
            return deepcopy(COMPLETE_PROPOSAL), deepcopy(MODEL_PROPOSAL_EVIDENCE)

        snapshot = self.classify(outer_classifier)

        self.assertEqual(1, outer_calls)
        self.assertEqual(0, inner_calls)
        self.assertIsNotNone(inner_error)
        self.assertEqual("classification_request_ambiguous", inner_error.code)
        self.assertEqual(
            snapshot,
            self.coordinator.require_authoritative_classification_snapshot(
                user_id="user-1",
                canonical_source_id=self.source_id,
            ),
        )


class SelectionAndLedgerTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_source_coordinator(self)
        self.assertTrue(
            hasattr(self.module, "build_selection_snapshot"),
            "build_selection_snapshot is absent",
        )
        for name in (
            "elect_transition_owner_from_snapshot",
            "create_or_verify_source_work_ledger",
        ):
            self.assertTrue(
                hasattr(self.module.SourceCoordinator, name),
                f"SourceCoordinator.{name} is absent",
            )

        self.fake = FakeFirestore()
        self.clock = MutableClock(FROZEN_NOW)
        self.uuids = SequentialUUIDs()
        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
        )
        identity = self.coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={"id": "graph-A"},
            evidence_kind="graph_hydration",
            thread_id="thread-1",
        )
        self.source_id = identity.canonical_source_id
        self.fake.events.clear()

    @property
    def classification_path(self):
        return f"users/user-1/sourceClassifications/{self.source_id}"

    @property
    def owner_path(self):
        return f"users/user-1/sourceTransitionOwners/{self.source_id}"

    @property
    def ledger_path(self):
        return f"users/user-1/sourceWorkLedgers/{self.source_id}"

    def write_events(self):
        return [
            event
            for event in self.fake.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def freeze(self, proposal):
        return self.coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=self.source_id,
            lease_seconds=60,
            classification_input=CLASSIFICATION_INPUT,
            classifier=lambda: (
                deepcopy(proposal),
                deepcopy(MODEL_PROPOSAL_EVIDENCE),
            ),
        )

    def elect(self, **kwargs):
        return self.coordinator.elect_transition_owner_from_snapshot(
            user_id="user-1",
            canonical_source_id=self.source_id,
            **kwargs,
        )

    def ledger(self):
        return self.coordinator.create_or_verify_source_work_ledger(
            user_id="user-1",
            canonical_source_id=self.source_id,
        )

    def test_preview_normalizes_unverified_optout_without_owner_key(self):
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "contact_optout", "claimed": True}
            ],
            "ordinaryObligations": [
                {"type": "field_update", "field": "stage", "value": "cold"},
                {"type": "informational", "message": "retained"},
            ],
        }
        preview = self.module.build_selection_snapshot(proposal)

        self.assertEqual("source-candidate-taxonomy-v1", preview["candidateTaxonomyVersion"])
        self.assertEqual("human_decision", preview["ownerKind"])
        self.assertNotIn("ownerKey", preview)
        self.assertEqual(
            "needs_user_input",
            preview["selectedCandidates"][0]["type"],
        )
        self.assertEqual(
            "unverified_optout_review",
            preview["selectedCandidates"][0]["reason"],
        )

        permuted = deepcopy(proposal)
        permuted["ordinaryObligations"].reverse()
        self.assertEqual(preview, self.module.build_selection_snapshot(permuted))

        unknown = {
            "schemaVersion": 1,
            "transitionCandidates": [{"type": "invented_transition"}],
            "ordinaryObligations": [],
        }
        with self.assertRaises(self.module.SourceCoordinatorConfigError):
            self.module.build_selection_snapshot(unknown)

    def test_terminal_owner_is_stored_and_expected_kind_is_assertion_only(self):
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "call_requested", "phone": "555-0100"},
                {"type": "property_unavailable", "property": "A"},
            ],
            "ordinaryObligations": [],
        }
        snapshot = self.freeze(proposal)
        self.fake.events.clear()

        with self.assertRaises(self.module.SourceCoordinatorConflict):
            self.elect(expected_owner_kind="human_decision")
        self.assertNotIn(self.owner_path, self.fake.data)
        self.assertEqual([], self.write_events())

        owner = self.elect(expected_owner_kind="terminal")
        self.assertEqual("terminal", owner["ownerKind"])
        self.assertIsNotNone(owner["ownerKey"])
        self.assertEqual(snapshot.selection_hash, owner["selectionHash"])
        self.assertEqual(1, owner["revision"])
        self.assertEqual(owner, self.fake.data[self.owner_path])
        self.assertEqual(["create"], [event[0] for event in self.write_events()])

        writes = list(self.write_events())
        retry = self.elect(expected_owner_kind="terminal")
        self.assertEqual(owner, retry)
        self.assertEqual(writes, self.write_events())

    def test_ordinary_only_source_persists_explicit_none_owner(self):
        self.freeze(
            {
                "schemaVersion": 1,
                "transitionCandidates": [],
                "ordinaryObligations": [
                    {"type": "field_update", "field": "stage", "value": "warm"}
                ],
            }
        )

        with self.assertRaises(self.module.TransitionOwnerConflict):
            self.ledger()
        self.assertNotIn(self.ledger_path, self.fake.data)

        owner = self.elect()

        self.assertEqual("none", owner["ownerKind"])
        self.assertIsNone(owner["ownerKey"])
        self.assertIn(self.owner_path, self.fake.data)

    def test_human_candidates_aggregate_and_model_optout_never_elects_hard(self):
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "call_requested", "phone": "555-0100"},
                {"type": "needs_user_input", "reason": "missing_property"},
                {"type": "contact_optout", "verified": True},
            ],
            "ordinaryObligations": [],
        }
        snapshot = self.freeze(proposal)
        owner = self.elect()

        self.assertEqual("human_decision", owner["ownerKind"])
        self.assertNotEqual("contact_optout", owner["ownerKind"])
        selected_types = {
            item["type"] for item in snapshot.selection_snapshot["selectedCandidates"]
        }
        self.assertEqual(
            {"call_requested", "needs_user_input"},
            selected_types,
        )
        self.assertNotIn(
            "contact_optout",
            json.dumps(self.module._thaw_json(snapshot.selection_snapshot)),
        )

    def test_ledger_applies_dominance_and_preserves_ordinary_work(self):
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "property_unavailable", "property": "A"},
                {"type": "call_requested", "phone": "555-0100"},
            ],
            "ordinaryObligations": [
                {"type": "generic_reply", "template": "terminal"},
                {"type": "field_update", "field": "stage", "value": "closed"},
                {"type": "new_property", "property": "B"},
                {"type": "informational", "message": "retained"},
            ],
        }
        self.freeze(proposal)
        owner = self.elect()
        self.fake.events.clear()

        ledger = self.ledger()
        by_type = {entry["kind"]: entry for entry in ledger["entries"]}

        self.assertEqual("terminal", owner["ownerKind"])
        self.assertEqual("delegate_owner", by_type["property_unavailable"]["dominanceOutcome"])
        self.assertEqual("dominated_by_owner", by_type["call_requested"]["dominanceOutcome"])
        self.assertEqual("delegate_terminal_policy", by_type["generic_reply"]["dominanceOutcome"])
        for kind in ("field_update", "new_property", "informational"):
            self.assertEqual("preserve", by_type[kind]["dominanceOutcome"])
        self.assertTrue(all(entry["state"] == "pending" for entry in ledger["entries"]))
        self.assertEqual(6, ledger["entryCount"])
        self.assertEqual(ledger, self.fake.data[self.ledger_path])
        self.assertEqual(1, len(self.write_events()))
        self.assertEqual("create", self.write_events()[0][0])
        self.assertEqual(self.ledger_path, self.write_events()[0][1])
        self.assertEqual(400, self.module.MAX_SOURCE_WORK_TRANSACTION_WRITES)

    def test_verified_hard_optout_dominates_other_transition_work(self):
        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=self.uuids,
            now_factory=self.clock,
            hard_optout_verifier=lambda _: deepcopy(HARD_OPTOUT_EVIDENCE),
        )
        self.coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=self.source_id,
            lease_seconds=60,
            classification_input=CLASSIFICATION_INPUT,
            classifier=lambda: self.fail("hard opt-out invoked classifier"),
        )
        classification = self.fake.data[self.classification_path]
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "contact_optout", "evidenceHash": "c" * 64},
                {"type": "close_conversation", "reason": "complete"},
                {"type": "call_requested", "phone": "555-0100"},
            ],
            "ordinaryObligations": [
                {"type": "generic_reply", "template": "ordinary"},
                {"type": "field_update", "field": "stage", "value": "closed"},
            ],
        }
        classification.update(
            self.module._build_classification_snapshot_material(
                canonical_source_id=self.source_id,
                classification_input_hash=classification["classificationInputHash"],
                model_request_key=None,
                complete_proposal=proposal,
                proposal_evidence=None,
                deterministic_evidence=deepcopy(HARD_OPTOUT_EVIDENCE),
            )
        )

        owner = self.elect()
        ledger = self.ledger()
        by_type = {entry["kind"]: entry for entry in ledger["entries"]}

        self.assertEqual("contact_optout", owner["ownerKind"])
        self.assertEqual("delegate_owner", by_type["contact_optout"]["dominanceOutcome"])
        self.assertEqual("dominated_by_owner", by_type["close_conversation"]["dominanceOutcome"])
        self.assertEqual("dominated_by_owner", by_type["call_requested"]["dominanceOutcome"])
        self.assertEqual("dominated_no_send", by_type["generic_reply"]["dominanceOutcome"])
        self.assertEqual("preserve", by_type["field_update"]["dominanceOutcome"])

    def test_duplicate_work_gets_stable_ordinals_and_full_hash_keys(self):
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [],
            "ordinaryObligations": [
                {"type": "field_update", "field": "stage", "value": "warm"},
                {"type": "field_update", "field": "stage", "value": "warm"},
                {"type": "informational", "message": "retained"},
            ],
        }

        def materialize(candidate):
            fake = FakeFirestore()
            coordinator = self.module.SourceCoordinator(
                fake,
                uuid_factory=SequentialUUIDs(),
                now_factory=MutableClock(FROZEN_NOW),
            )
            identity = coordinator.admit_or_repair_source_identity(
                user_id="user-1",
                hydrated_message={"id": "graph-A"},
                evidence_kind="graph_hydration",
                thread_id="thread-1",
            )
            coordinator.classify_source_once(
                user_id="user-1",
                canonical_source_id=identity.canonical_source_id,
                lease_seconds=60,
                classification_input=CLASSIFICATION_INPUT,
                classifier=lambda: (
                    deepcopy(candidate),
                    deepcopy(MODEL_PROPOSAL_EVIDENCE),
                ),
            )
            coordinator.elect_transition_owner_from_snapshot(
                user_id="user-1",
                canonical_source_id=identity.canonical_source_id,
            )
            return coordinator.create_or_verify_source_work_ledger(
                user_id="user-1",
                canonical_source_id=identity.canonical_source_id,
            )

        first = materialize(proposal)
        permuted = deepcopy(proposal)
        permuted["ordinaryObligations"].reverse()
        second = materialize(permuted)

        duplicates = [
            entry for entry in first["entries"] if entry["kind"] == "field_update"
        ]
        self.assertEqual([1, 2], [entry["occurrenceOrdinal"] for entry in duplicates])
        self.assertEqual(2, len({entry["workKey"] for entry in duplicates}))
        self.assertTrue(all(len(entry["workKey"]) == 64 for entry in duplicates))
        self.assertEqual(first["ledgerHash"], second["ledgerHash"])
        self.assertEqual(
            [entry["workKey"] for entry in first["entries"]],
            [entry["workKey"] for entry in second["entries"]],
        )

    def test_entry_byte_and_write_limits_fail_before_ledger_mutation(self):
        oversized_entries = {
            "schemaVersion": 1,
            "transitionCandidates": [],
            "ordinaryObligations": [
                {"type": "informational", "index": index}
                for index in range(129)
            ],
        }
        self.freeze(oversized_entries)
        self.elect()
        before_data = deepcopy(self.fake.data)
        self.fake.events.clear()

        with self.assertRaises(self.module.SourceCoordinatorConfigError):
            self.ledger()
        self.assertEqual(before_data, self.fake.data)
        self.assertNotIn(self.ledger_path, self.fake.data)
        self.assertEqual([], self.write_events())

        fake = FakeFirestore()
        coordinator = self.module.SourceCoordinator(
            fake,
            uuid_factory=SequentialUUIDs(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        identity = coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={"id": "graph-B"},
            evidence_kind="graph_hydration",
            thread_id="thread-2",
        )
        coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=identity.canonical_source_id,
            lease_seconds=60,
            classification_input=CLASSIFICATION_INPUT,
            classifier=lambda: (
                deepcopy(COMPLETE_PROPOSAL),
                deepcopy(MODEL_PROPOSAL_EVIDENCE),
            ),
        )
        coordinator.elect_transition_owner_from_snapshot(
            user_id="user-1",
            canonical_source_id=identity.canonical_source_id,
        )
        before_data = deepcopy(fake.data)
        before_writes = [event for event in fake.events if event[0] == "create"]
        with mock.patch.object(self.module, "MAX_SOURCE_WORK_LEDGER_BYTES", 1):
            with self.assertRaises(self.module.SourceCoordinatorConfigError):
                coordinator.create_or_verify_source_work_ledger(
                    user_id="user-1",
                    canonical_source_id=identity.canonical_source_id,
                )
        self.assertEqual(before_data, fake.data)
        self.assertEqual(
            before_writes,
            [event for event in fake.events if event[0] == "create"],
        )
        with mock.patch.object(
            self.module,
            "MAX_SOURCE_WORK_TRANSACTION_WRITES",
            0,
        ):
            with self.assertRaises(self.module.SourceCoordinatorConfigError):
                coordinator.create_or_verify_source_work_ledger(
                    user_id="user-1",
                    canonical_source_id=identity.canonical_source_id,
                )
        self.assertEqual(before_data, fake.data)
        self.assertEqual(
            before_writes,
            [event for event in fake.events if event[0] == "create"],
        )

    def test_apply_then_raise_is_accepted_only_by_exact_readback(self):
        self.freeze(deepcopy(COMPLETE_PROPOSAL))
        self.fake.events.clear()
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown owner commit")

        owner = self.elect()

        self.assertEqual(owner, self.fake.data[self.owner_path])
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown ledger commit")
        ledger = self.ledger()
        self.assertEqual(ledger, self.fake.data[self.ledger_path])
        self.assertEqual(
            2,
            sum(event[0] == "commit_raised_after_apply" for event in self.fake.events),
        )

    def test_unauthorized_settlement_or_conflicting_owner_cannot_replace_ledger(self):
        self.freeze(deepcopy(COMPLETE_PROPOSAL))
        self.elect()
        ledger = self.ledger()
        writes = list(self.write_events())

        self.fake.data[self.ledger_path]["entries"][0]["state"] = "applying"
        retry = self.ledger()
        self.assertEqual("applying", retry["entries"][0]["state"])
        self.assertEqual(ledger["ledgerHash"], retry["ledgerHash"])
        self.assertEqual(writes, self.write_events())

        for unsupported_state in ("completed", "delegated", "dominated"):
            self.fake.data[self.ledger_path]["entries"][0]["state"] = (
                unsupported_state
            )
            with self.subTest(state=unsupported_state):
                with self.assertRaises(self.module.SourceWorkLedgerConflict):
                    self.ledger()
                self.assertEqual(writes, self.write_events())

        self.fake.data[self.ledger_path]["entries"][0]["state"] = "applying"

        self.fake.data[self.owner_path]["ownerKind"] = "terminal"
        with self.assertRaises(self.module.SourceCoordinatorError):
            self.ledger()
        self.assertEqual(writes, self.write_events())
        self.assertEqual(ledger["ledgerHash"], self.fake.data[self.ledger_path]["ledgerHash"])


class ThreadHeadAndWakeTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_source_coordinator(self)
        for name in (
            "admit_pending_inbound",
            "enqueue_blocked_source",
            "claim_or_block_thread_transition",
            "release_generation_and_wake_oldest",
            "claim_wake_and_rebind_generation",
        ):
            self.assertTrue(
                hasattr(self.module.SourceCoordinator, name),
                f"SourceCoordinator.{name} is absent",
            )

        self.fake = FakeFirestore()
        self.clock = MutableClock(FROZEN_NOW)
        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=SequentialUUIDs(),
            now_factory=self.clock,
        )

    def prepare_authority(self, graph_id, *, thread_id="thread-queue"):
        identity = self.coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        self.coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=identity.canonical_source_id,
            lease_seconds=60,
            classification_input={
                "schemaVersion": 1,
                "message": {"id": graph_id},
            },
            classifier=lambda: (
                deepcopy(COMPLETE_PROPOSAL),
                deepcopy(MODEL_PROPOSAL_EVIDENCE),
            ),
        )
        self.coordinator.elect_transition_owner_from_snapshot(
            user_id="user-1",
            canonical_source_id=identity.canonical_source_id,
        )
        self.coordinator.create_or_verify_source_work_ledger(
            user_id="user-1",
            canonical_source_id=identity.canonical_source_id,
        )
        return identity.canonical_source_id

    def prepare_none_authority(self, graph_id, *, thread_id="thread-queue"):
        identity = self.coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={"id": graph_id},
            evidence_kind="graph_hydration",
            thread_id=thread_id,
        )
        source_id = identity.canonical_source_id
        self.coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=source_id,
            lease_seconds=60,
            classification_input={
                "schemaVersion": 1,
                "message": {"id": graph_id},
            },
            classifier=lambda: (
                {
                    "schemaVersion": 1,
                    "transitionCandidates": [],
                    "ordinaryObligations": [
                        {
                            "type": "field_update",
                            "field": "stage",
                            "value": "warm",
                        }
                    ],
                },
                deepcopy(MODEL_PROPOSAL_EVIDENCE),
            ),
        )
        owner = self.coordinator.elect_transition_owner_from_snapshot(
            user_id="user-1",
            canonical_source_id=source_id,
        )
        self.assertEqual("none", owner["ownerKind"])
        self.coordinator.create_or_verify_source_work_ledger(
            user_id="user-1",
            canonical_source_id=source_id,
        )
        return source_id

    def admission_arguments(
        self,
        source_id,
        *,
        received_offset=0,
        sent_offset=0,
    ):
        return {
            "user_id": "user-1",
            "canonical_source_id": source_id,
            "received_at": FROZEN_NOW + timedelta(seconds=received_offset),
            "sent_at": FROZEN_NOW + timedelta(seconds=sent_offset),
            "saved_history_binding": {
                "schemaVersion": 1,
                "historyKey": f"history-{source_id}",
            },
            "index_binding": {
                "schemaVersion": 1,
                "indexKey": f"index-{source_id}",
            },
        }

    def admit(self, source_id, **offsets):
        return self.coordinator.admit_pending_inbound(
            **self.admission_arguments(source_id, **offsets)
        )

    def write_events(self):
        return [
            event
            for event in self.fake.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def test_two_worker_race_leaves_one_head_and_one_durable_block(self):
        first = self.prepare_authority("graph-race-a")
        second = self.prepare_authority("graph-race-b")
        self.admit(first, received_offset=1, sent_offset=1)
        self.admit(second, received_offset=2, sent_offset=2)
        self.fake.events.clear()
        self.fake.before_commit_barrier = Barrier(2)
        results = []
        errors = []

        def contend(source_id):
            try:
                results.append(
                    self.coordinator.claim_or_block_thread_transition(
                        user_id="user-1",
                        canonical_source_id=source_id,
                    )
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        workers = [Thread(target=contend, args=(source_id,)) for source_id in (first, second)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual([], errors)
        self.assertEqual({"claimed", "blocked"}, {result.disposition for result in results})
        head_path = "users/user-1/threadTransitionHeads/thread-queue"
        head = self.fake.data[head_path]
        winner = head["activeCanonicalSourceId"]
        loser = second if winner == first else first
        admission_path = f"users/user-1/inboundPendingAdmissions/{loser}"
        projection_path = f"users/user-1/blockedSources/{loser}"
        admission = self.fake.data[admission_path]
        projection = self.fake.data[projection_path]

        self.assertEqual("active", head["activeState"])
        self.assertEqual(1, head["activeGeneration"])
        self.assertEqual("blocked", admission["admissionState"])
        self.assertEqual("blocked", admission["blockedLifecycleState"])
        self.assertEqual(winner, admission["currentBlocker"]["canonicalSourceId"])
        self.assertEqual(
            admission["currentBlocker"]["headHash"],
            projection["currentBlocker"]["headHash"],
        )
        forbidden_fragments = (
            "processedMessages",
            "sourceSettlements",
            "cursor",
            "domain",
            "notification",
        )
        written_paths = [event[1] for event in self.write_events()]
        self.assertFalse(
            any(fragment in path for fragment in forbidden_fragments for path in written_paths)
        )

    def test_same_source_retry_tamper_and_apply_then_raise_are_strict(self):
        source_id = self.prepare_authority("graph-strict-head")
        self.admit(source_id)
        self.fake.events.clear()
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown head commit")

        claimed = self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=source_id,
        )

        self.assertEqual("claimed", claimed.disposition)
        head_path = "users/user-1/threadTransitionHeads/thread-queue"
        self.assertEqual(source_id, self.fake.data[head_path]["activeCanonicalSourceId"])
        writes = list(self.write_events())
        retry = self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=source_id,
        )
        self.assertEqual(claimed, retry)
        self.assertEqual(writes, self.write_events())

        self.fake.data[head_path]["activeOwnerKey"] = "f" * 64
        with self.assertRaises(self.module.ThreadTransitionConflict):
            self.coordinator.claim_or_block_thread_transition(
                user_id="user-1",
                canonical_source_id=source_id,
            )
        self.assertEqual(writes, self.write_events())

    def test_queue_bound_fails_before_source_101_materialization(self):
        self.assertEqual(100, self.module.MAX_BLOCKED_SOURCES_PER_THREAD)
        active = self.prepare_authority("graph-bound-active")
        self.admit(active)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )

        with mock.patch.object(
            self.module,
            "MAX_BLOCKED_SOURCES_PER_THREAD",
            2,
        ):
            for index in range(2):
                source_id = self.prepare_authority(f"graph-bound-{index}")
                result = self.coordinator.enqueue_blocked_source(
                    **self.admission_arguments(
                        source_id,
                        received_offset=index + 1,
                        sent_offset=index + 1,
                    )
                )
                self.assertEqual("blocked", result.disposition)

            overflow = self.prepare_authority("graph-bound-overflow")
            before = deepcopy(self.fake.data)
            self.fake.events.clear()
            with self.assertRaises(self.module.ThreadQueueLimitExceeded):
                self.coordinator.enqueue_blocked_source(
                    **self.admission_arguments(
                        overflow,
                        received_offset=3,
                        sent_offset=3,
                    )
                )
            self.assertEqual(before, self.fake.data)
            self.assertEqual([], self.write_events())
            self.assertNotIn(
                f"users/user-1/inboundPendingAdmissions/{overflow}",
                self.fake.data,
            )
            self.assertNotIn(
                f"users/user-1/blockedSources/{overflow}",
                self.fake.data,
            )

    def test_occupied_head_requires_atomic_enqueue_and_pending_blocks_release(self):
        active = self.prepare_authority("graph-pending-active")
        pending = self.prepare_authority("graph-pending-existing")
        self.admit(active)
        self.admit(pending, received_offset=1, sent_offset=1)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )
        late = self.prepare_authority("graph-pending-late")
        self.fake.events.clear()

        with self.assertRaises(self.module.ThreadTransitionConflict):
            self.admit(late, received_offset=2, sent_offset=2)

        self.assertNotIn(
            f"users/user-1/inboundPendingAdmissions/{late}",
            self.fake.data,
        )
        self.assertEqual([], self.write_events())
        active_path = f"users/user-1/inboundPendingAdmissions/{active}"
        self.fake.data[active_path]["admissionState"] = "settled"
        head_before = deepcopy(
            self.fake.data["users/user-1/threadTransitionHeads/thread-queue"]
        )
        self.fake.events.clear()

        with self.assertRaises(self.module.WakeReleaseConflict):
            self.coordinator.release_generation_and_wake_oldest(
                user_id="user-1",
                thread_id="thread-queue",
                canonical_source_id=active,
            )

        self.assertEqual(
            head_before,
            self.fake.data["users/user-1/threadTransitionHeads/thread-queue"],
        )
        self.assertEqual("pending", self.fake.data[
            f"users/user-1/inboundPendingAdmissions/{pending}"
        ]["admissionState"])
        self.assertEqual([], self.write_events())

    def test_none_owner_admits_pending_while_another_source_holds_head(self):
        active = self.prepare_authority("graph-none-admit-active")
        self.admit(active)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )
        head_path = "users/user-1/threadTransitionHeads/thread-queue"
        head_before = deepcopy(self.fake.data[head_path])
        ordinary = self.prepare_none_authority("graph-none-admit-ordinary")
        self.fake.events.clear()

        admitted = self.coordinator.admit_pending_inbound(
            **self.admission_arguments(
                ordinary,
                received_offset=1,
                sent_offset=1,
            )
        )

        self.assertEqual("pending", admitted.state)
        self.assertEqual(head_before, self.fake.data[head_path])
        self.assertNotIn(
            f"users/user-1/blockedSources/{ordinary}",
            self.fake.data,
        )
        active_path = f"users/user-1/inboundPendingAdmissions/{active}"
        self.fake.data[active_path]["admissionState"] = "settled"
        self.fake.events.clear()

        released = self.coordinator.release_generation_and_wake_oldest(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=active,
        )

        self.assertEqual("clear", released.head_state)
        self.assertEqual(
            "pending",
            self.fake.data[
                f"users/user-1/inboundPendingAdmissions/{ordinary}"
            ]["admissionState"],
        )
        writes = list(self.write_events())
        with self.assertRaises(self.module.WakeReleaseConflict):
            self.coordinator.release_generation_and_wake_oldest(
                user_id="user-1",
                thread_id="thread-queue",
                canonical_source_id=active,
            )
        self.assertEqual(writes, self.write_events())

    def test_release_chooses_oldest_and_claim_rebinds_all_remaining(self):
        active = self.prepare_authority("graph-wake-active")
        self.admit(active)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )
        candidates = []
        for graph_id, received_offset, sent_offset in (
            ("graph-wake-late", 20, 10),
            ("graph-wake-tie-late", 10, 8),
            ("graph-wake-oldest", 10, 5),
        ):
            source_id = self.prepare_authority(graph_id)
            candidates.append(source_id)
            self.coordinator.enqueue_blocked_source(
                **self.admission_arguments(
                    source_id,
                    received_offset=received_offset,
                    sent_offset=sent_offset,
                )
            )
        active_path = f"users/user-1/inboundPendingAdmissions/{active}"
        self.fake.data[active_path]["admissionState"] = "settled"

        released = self.coordinator.release_generation_and_wake_oldest(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=active,
        )

        oldest = candidates[2]
        self.assertEqual(oldest, released.next_canonical_source_id)
        self.assertEqual(2, released.wake_generation)
        oldest_path = f"users/user-1/inboundPendingAdmissions/{oldest}"
        self.assertEqual("eligible", self.fake.data[oldest_path]["wakeState"])
        self.assertEqual(released.wake_token, self.fake.data[oldest_path]["wakeToken"])
        release_writes = list(self.write_events())
        release_retry = self.coordinator.release_generation_and_wake_oldest(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=active,
        )
        self.assertEqual(released, release_retry)
        self.assertEqual(release_writes, self.write_events())

        claimed = self.coordinator.claim_wake_and_rebind_generation(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=oldest,
            wake_token=released.wake_token,
            wake_claim_id="wake-claim-1",
        )

        self.assertEqual(oldest, claimed.canonical_source_id)
        head = self.fake.data["users/user-1/threadTransitionHeads/thread-queue"]
        self.assertEqual(oldest, head["activeCanonicalSourceId"])
        self.assertEqual(2, head["activeGeneration"])
        self.assertEqual(
            "settled_as_new_blocker",
            self.fake.data[oldest_path]["blockedLifecycleState"],
        )
        self.assertEqual("consumed", self.fake.data[oldest_path]["wakeState"])
        for remaining in candidates[:2]:
            path = f"users/user-1/inboundPendingAdmissions/{remaining}"
            projection = f"users/user-1/blockedSources/{remaining}"
            self.assertEqual(
                oldest,
                self.fake.data[path]["currentBlocker"]["canonicalSourceId"],
            )
            self.assertEqual(2, self.fake.data[path]["currentBlocker"]["generation"])
            self.assertEqual(
                self.fake.data[path]["currentBlocker"]["headHash"],
                self.fake.data[projection]["currentBlocker"]["headHash"],
            )
            self.assertEqual("none", self.fake.data[path]["wakeState"])
        ordinary = self.prepare_none_authority("graph-wake-ordinary")
        self.admit(ordinary, received_offset=30, sent_offset=30)
        self.fake.events.clear()
        claim_retry = self.coordinator.claim_wake_and_rebind_generation(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=oldest,
            wake_token=released.wake_token,
            wake_claim_id="wake-claim-1",
        )
        self.assertEqual(claimed, claim_retry)
        self.assertEqual([], self.write_events())

    def test_empty_queue_release_clears_head_and_retry_fails_closed(self):
        active = self.prepare_authority("graph-wake-empty")
        self.admit(active)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )
        active_path = f"users/user-1/inboundPendingAdmissions/{active}"
        self.fake.data[active_path]["admissionState"] = "settled"
        self.fake.events.clear()

        released = self.coordinator.release_generation_and_wake_oldest(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=active,
        )

        self.assertEqual("clear", released.head_state)
        self.assertIsNone(released.next_canonical_source_id)
        head = self.fake.data["users/user-1/threadTransitionHeads/thread-queue"]
        self.assertEqual("clear", head["activeState"])
        self.assertIsNone(head["activeCanonicalSourceId"])
        writes = list(self.write_events())
        with self.assertRaises(self.module.WakeReleaseConflict):
            self.coordinator.release_generation_and_wake_oldest(
                user_id="user-1",
                thread_id="thread-queue",
                canonical_source_id=active,
            )
        self.assertEqual(writes, self.write_events())

    def test_two_workers_racing_wake_claim_leave_one_complete_rebind(self):
        active = self.prepare_authority("graph-wake-race-active")
        self.admit(active)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )
        blocked = []
        for index in range(3):
            source_id = self.prepare_authority(f"graph-wake-race-{index}")
            blocked.append(source_id)
            self.coordinator.enqueue_blocked_source(
                **self.admission_arguments(
                    source_id,
                    received_offset=index + 1,
                    sent_offset=index + 1,
                )
            )
        active_path = f"users/user-1/inboundPendingAdmissions/{active}"
        self.fake.data[active_path]["admissionState"] = "settled"
        released = self.coordinator.release_generation_and_wake_oldest(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=active,
        )
        selected = blocked[0]
        self.fake.before_commit_barrier = Barrier(2)
        results = []
        errors = []

        def contend(claim_id):
            try:
                results.append(
                    self.coordinator.claim_wake_and_rebind_generation(
                        user_id="user-1",
                        thread_id="thread-queue",
                        canonical_source_id=selected,
                        wake_token=released.wake_token,
                        wake_claim_id=claim_id,
                    )
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        workers = [
            Thread(target=contend, args=(claim_id,))
            for claim_id in ("wake-claim-race-a", "wake-claim-race-b")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], self.module.WakeClaimConflict)
        winner = results[0]
        head = self.fake.data["users/user-1/threadTransitionHeads/thread-queue"]
        self.assertEqual(selected, head["activeCanonicalSourceId"])
        self.assertEqual(2, head["activeGeneration"])
        selected_path = f"users/user-1/inboundPendingAdmissions/{selected}"
        self.assertEqual("consumed", self.fake.data[selected_path]["wakeState"])
        self.assertEqual(winner.wake_claim_id, self.fake.data[selected_path]["wakeClaimId"])
        for remaining in blocked[1:]:
            admission = self.fake.data[
                f"users/user-1/inboundPendingAdmissions/{remaining}"
            ]
            projection = self.fake.data[
                f"users/user-1/blockedSources/{remaining}"
            ]
            self.assertEqual(selected, admission["currentBlocker"]["canonicalSourceId"])
            self.assertEqual(2, admission["currentBlocker"]["generation"])
            self.assertEqual(admission["currentBlocker"], projection["currentBlocker"])

    def test_wake_apply_then_raise_and_competing_claim_are_fenced(self):
        active = self.prepare_authority("graph-wake-fence-active")
        self.admit(active)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )
        blocked = self.prepare_authority("graph-wake-fence-blocked")
        self.coordinator.enqueue_blocked_source(
            **self.admission_arguments(blocked, received_offset=1, sent_offset=1)
        )
        active_path = f"users/user-1/inboundPendingAdmissions/{active}"
        self.fake.data[active_path]["admissionState"] = "settled"
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown release")

        released = self.coordinator.release_generation_and_wake_oldest(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=active,
        )

        self.fake.apply_then_raise_next_commit = RuntimeError("unknown wake claim")
        claimed = self.coordinator.claim_wake_and_rebind_generation(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=blocked,
            wake_token=released.wake_token,
            wake_claim_id="wake-claim-winner",
        )
        self.assertEqual("wake-claim-winner", claimed.wake_claim_id)
        with self.assertRaises(self.module.WakeClaimConflict):
            self.coordinator.claim_wake_and_rebind_generation(
                user_id="user-1",
                thread_id="thread-queue",
                canonical_source_id=blocked,
                wake_token=released.wake_token,
                wake_claim_id="wake-claim-loser",
            )
        admission_path = f"users/user-1/inboundPendingAdmissions/{blocked}"
        projection_path = f"users/user-1/blockedSources/{blocked}"
        forged_admission = deepcopy(self.fake.data[admission_path])
        forged_admission["wakeToken"] = "f" * 64
        forged_admission["wakeClaimId"] = "wake-claim-forged"
        self.fake.data[admission_path] = forged_admission
        projection_before = self.fake.data[projection_path]
        self.fake.data[projection_path] = (
            self.module._blocked_projection_from_admission(
                forged_admission,
                now=projection_before["updatedAt"],
                created_at=projection_before["createdAt"],
            )
        )
        before = deepcopy(self.fake.data)
        self.fake.events.clear()

        with self.assertRaises(self.module.WakeClaimConflict):
            self.coordinator.claim_wake_and_rebind_generation(
                user_id="user-1",
                thread_id="thread-queue",
                canonical_source_id=blocked,
                wake_token="f" * 64,
                wake_claim_id="wake-claim-forged",
            )

        self.assertEqual(before, self.fake.data)
        self.assertEqual([], self.write_events())

    def test_wake_rebind_rejects_foreign_remaining_blocker(self):
        active = self.prepare_authority("graph-rebind-active")
        self.admit(active)
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=active,
        )
        selected = self.prepare_authority("graph-rebind-selected")
        remaining = self.prepare_authority("graph-rebind-remaining")
        self.coordinator.enqueue_blocked_source(
            **self.admission_arguments(selected, received_offset=1, sent_offset=1)
        )
        self.coordinator.enqueue_blocked_source(
            **self.admission_arguments(remaining, received_offset=2, sent_offset=2)
        )
        active_path = f"users/user-1/inboundPendingAdmissions/{active}"
        self.fake.data[active_path]["admissionState"] = "settled"
        released = self.coordinator.release_generation_and_wake_oldest(
            user_id="user-1",
            thread_id="thread-queue",
            canonical_source_id=active,
        )
        remaining_path = f"users/user-1/inboundPendingAdmissions/{remaining}"
        projection_path = f"users/user-1/blockedSources/{remaining}"
        foreign_blocker = deepcopy(
            self.fake.data[remaining_path]["currentBlocker"]
        )
        foreign_blocker.update(
            {
                "canonicalSourceId": "source-foreign",
                "generation": 99,
                "threadHeadRevision": 99,
                "headHash": "f" * 64,
            }
        )
        self.fake.data[remaining_path]["currentBlocker"] = deepcopy(
            foreign_blocker
        )
        self.fake.data[projection_path]["currentBlocker"] = deepcopy(
            foreign_blocker
        )
        before = deepcopy(self.fake.data)
        self.fake.events.clear()

        with self.assertRaises(self.module.WakeClaimConflict):
            self.coordinator.claim_wake_and_rebind_generation(
                user_id="user-1",
                thread_id="thread-queue",
                canonical_source_id=selected,
                wake_token=released.wake_token,
                wake_claim_id="wake-claim-foreign-blocker",
            )

        self.assertEqual(before, self.fake.data)
        self.assertEqual([], self.write_events())


class LedgerTransitionAndSettlementTests(unittest.TestCase):
    def setUp(self):
        self.module = _load_source_coordinator(self)
        for name in (
            "create_or_verify_deferred_work",
            "record_source_work_applying",
            "complete_source_work_entry",
            "delegate_source_work_entry",
            "dominate_source_work_entry_from_selection",
            "settle_source_markers_if_ready",
        ):
            self.assertTrue(
                hasattr(self.module.SourceCoordinator, name),
                f"SourceCoordinator.{name} is absent",
            )

        self.fake = FakeFirestore()
        self.clock = MutableClock(FROZEN_NOW)
        self.coordinator = self.module.SourceCoordinator(
            self.fake,
            uuid_factory=SequentialUUIDs(),
            now_factory=self.clock,
        )
        identity = self.coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={
                "id": "graph-ledger",
                "internetMessageId": "<ledger@example.test>",
            },
            evidence_kind="graph_hydration",
            thread_id="thread-ledger",
        )
        self.source_id = identity.canonical_source_id
        proposal = {
            "schemaVersion": 1,
            "transitionCandidates": [
                {"type": "property_unavailable", "property": "A"},
                {"type": "call_requested", "phone": "555-0100"},
            ],
            "ordinaryObligations": [
                {"type": "generic_reply", "template": "terminal"},
                {"type": "field_update", "field": "stage", "value": "closed"},
                {"type": "informational", "message": "retained"},
            ],
        }
        self.coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=self.source_id,
            lease_seconds=60,
            classification_input=deepcopy(CLASSIFICATION_INPUT),
            classifier=lambda: (
                deepcopy(proposal),
                deepcopy(MODEL_PROPOSAL_EVIDENCE),
            ),
        )
        self.coordinator.elect_transition_owner_from_snapshot(
            user_id="user-1",
            canonical_source_id=self.source_id,
        )
        self.ledger = self.coordinator.create_or_verify_source_work_ledger(
            user_id="user-1",
            canonical_source_id=self.source_id,
        )
        self.coordinator.admit_pending_inbound(
            user_id="user-1",
            canonical_source_id=self.source_id,
            received_at=FROZEN_NOW,
            sent_at=FROZEN_NOW,
            saved_history_binding={
                "schemaVersion": 1,
                "historyKey": "history-ledger",
            },
            index_binding={
                "schemaVersion": 1,
                "indexKey": "index-ledger",
            },
        )
        self.coordinator.claim_or_block_thread_transition(
            user_id="user-1",
            canonical_source_id=self.source_id,
        )
        self.fake.events.clear()

    @property
    def ledger_path(self):
        return f"users/user-1/sourceWorkLedgers/{self.source_id}"

    @property
    def admission_path(self):
        return f"users/user-1/inboundPendingAdmissions/{self.source_id}"

    def entry(self, kind):
        return next(entry for entry in self.ledger["entries"] if entry["kind"] == kind)

    def work_arguments(self, kind):
        entry = self.entry(kind)
        return {
            "user_id": "user-1",
            "canonical_source_id": self.source_id,
            "ledger_hash": self.ledger["ledgerHash"],
            "work_key": entry["workKey"],
            "payload_hash": entry["payloadHash"],
        }

    def completion_record(self, kind="field_update", result_hash="d" * 64):
        return {
            "schemaVersion": 1,
            "evidenceKind": "work_completion",
            "workKind": kind,
            "resultHash": result_hash,
        }

    def write_events(self):
        return [
            event
            for event in self.fake.events
            if event[0] in {"create", "set", "update", "delete"}
        ]

    def stored_entry(self, kind):
        return next(
            entry
            for entry in self.fake.data[self.ledger_path]["entries"]
            if entry["kind"] == kind
        )

    def test_ledger_transition_graph_and_exact_completion_evidence(self):
        applying = self.coordinator.record_source_work_applying(
            **self.work_arguments("field_update")
        )
        self.assertEqual("applying", applying.state)
        self.assertIsNone(self.stored_entry("field_update")["resolutionEvidence"])
        writes = list(self.write_events())
        retry = self.coordinator.record_source_work_applying(
            **self.work_arguments("field_update")
        )
        self.assertEqual(applying, retry)
        self.assertEqual(writes, self.write_events())

        with self.assertRaises(self.module.SourceWorkTransitionConflict):
            self.coordinator.complete_source_work_entry(
                **self.work_arguments("informational"),
                completion_record=self.completion_record("informational"),
            )

        completed = self.coordinator.complete_source_work_entry(
            **self.work_arguments("field_update"),
            completion_record=self.completion_record(),
        )
        self.assertEqual("completed", completed.state)
        stored = self.stored_entry("field_update")
        self.assertEqual("work_completion", stored["resolutionEvidence"]["evidenceKind"])
        self.assertEqual(completed.evidence_hash, stored["resolutionEvidenceHash"])
        writes = list(self.write_events())
        exact_retry = self.coordinator.complete_source_work_entry(
            **self.work_arguments("field_update"),
            completion_record=self.completion_record(),
        )
        self.assertEqual(completed, exact_retry)
        self.assertEqual(writes, self.write_events())

        with self.assertRaises(self.module.SourceWorkTransitionConflict):
            self.coordinator.record_source_work_applying(
                **self.work_arguments("field_update")
            )

    def test_wrong_work_bindings_and_completion_schema_write_nothing(self):
        before = deepcopy(self.fake.data)
        bad_arguments = self.work_arguments("field_update")
        bad_arguments["ledger_hash"] = "0" * 64
        with self.assertRaises(self.module.SourceWorkTransitionConflict):
            self.coordinator.record_source_work_applying(**bad_arguments)
        with self.assertRaises(self.module.SourceCoordinatorConfigError):
            self.coordinator.complete_source_work_entry(
                **self.work_arguments("field_update"),
                completion_record={
                    "schemaVersion": 1,
                    "evidenceKind": "handled_log",
                    "workKind": "field_update",
                    "resultHash": "d" * 64,
                },
            )
        self.assertEqual(before, self.fake.data)
        self.assertEqual([], self.write_events())

    def test_delegation_is_atomic_idempotent_and_split_brain_blocks(self):
        delegated = self.coordinator.delegate_source_work_entry(
            **self.work_arguments("property_unavailable")
        )
        entry = self.stored_entry("property_unavailable")
        deferred_path = f"users/user-1/sourceDeferredWork/{entry['workKey']}"

        self.assertEqual("delegated", entry["state"])
        self.assertEqual("deferred", self.fake.data[deferred_path]["state"])
        self.assertEqual(delegated.binding_hash, self.fake.data[deferred_path]["bindingHash"])
        self.assertEqual(
            self.fake.data[deferred_path]["bindingHash"],
            entry["resolutionEvidence"]["deferredBindingHash"],
        )
        writes = list(self.write_events())
        retry = self.coordinator.create_or_verify_deferred_work(
            **self.work_arguments("property_unavailable")
        )
        self.assertEqual(delegated, retry)
        self.assertEqual(writes, self.write_events())

        del self.fake.data[deferred_path]
        with self.assertRaises(self.module.DeferredWorkConflict):
            self.coordinator.delegate_source_work_entry(
                **self.work_arguments("property_unavailable")
            )
        self.assertEqual(writes, self.write_events())

    def test_dominance_is_derived_from_selection_and_preserve_cannot_dominate(self):
        dominated = self.coordinator.dominate_source_work_entry_from_selection(
            **self.work_arguments("call_requested")
        )
        entry = self.stored_entry("call_requested")
        self.assertEqual("dominated", dominated.state)
        self.assertEqual("selection_dominance", entry["resolutionEvidence"]["evidenceKind"])
        self.assertEqual("terminal", entry["resolutionEvidence"]["dominatingOwnerKind"])

        with self.assertRaises(self.module.SourceWorkTransitionConflict):
            self.coordinator.dominate_source_work_entry_from_selection(
                **self.work_arguments("informational")
            )

    def test_ledger_transition_apply_then_raise_uses_exact_readback(self):
        self.fake.apply_then_raise_next_commit = RuntimeError("unknown applying")
        applying = self.coordinator.record_source_work_applying(
            **self.work_arguments("field_update")
        )
        self.assertEqual("applying", applying.state)

        self.fake.apply_then_raise_next_commit = RuntimeError("unknown delegation")
        delegated = self.coordinator.delegate_source_work_entry(
            **self.work_arguments("generic_reply")
        )
        self.assertEqual("delegated", delegated.ledger_state)

    def settle_all_work(self):
        for kind, result_hash in (
            ("field_update", "d" * 64),
            ("informational", "e" * 64),
        ):
            self.coordinator.record_source_work_applying(
                **self.work_arguments(kind)
            )
            self.coordinator.complete_source_work_entry(
                **self.work_arguments(kind),
                completion_record=self.completion_record(kind, result_hash),
            )
        for kind in ("property_unavailable", "generic_reply"):
            self.coordinator.delegate_source_work_entry(
                **self.work_arguments(kind)
            )
        self.coordinator.dominate_source_work_entry_from_selection(
            **self.work_arguments("call_requested")
        )

    def settle(self):
        return self.coordinator.settle_source_markers_if_ready(
            user_id="user-1",
            canonical_source_id=self.source_id,
            ledger_hash=self.ledger["ledgerHash"],
        )

    def test_settlement_rejects_pending_applying_and_missing_head(self):
        before = deepcopy(self.fake.data)
        with self.assertRaises(self.module.SourceSettlementNotReady):
            self.settle()
        self.assertEqual(before, self.fake.data)
        self.assertEqual([], self.write_events())

        self.coordinator.record_source_work_applying(
            **self.work_arguments("field_update")
        )
        self.fake.events.clear()
        with self.assertRaises(self.module.SourceSettlementNotReady):
            self.settle()
        self.assertEqual([], self.write_events())

        self.settle_all_work()
        head_path = "users/user-1/threadTransitionHeads/thread-ledger"
        del self.fake.data[head_path]
        self.fake.events.clear()
        with self.assertRaises(self.module.SourceSettlementNotReady):
            self.settle()
        self.assertEqual([], self.write_events())

    def test_settlement_rejects_missing_malformed_or_conflicting_authority(self):
        self.settle_all_work()
        prefix = "users/user-1"
        identity_path = f"{prefix}/sourceIdentities/{self.source_id}"
        classification_path = f"{prefix}/sourceClassifications/{self.source_id}"
        owner_path = f"{prefix}/sourceTransitionOwners/{self.source_id}"
        ledger_path = f"{prefix}/sourceWorkLedgers/{self.source_id}"
        head_path = f"{prefix}/threadTransitionHeads/thread-ledger"
        settlement_path = f"{prefix}/sourceSettlements/{self.source_id}"
        baseline = deepcopy(self.fake.data)

        def remove(path):
            return lambda data: data.pop(path)

        def replace_field(path, field, value):
            return lambda data: data[path].__setitem__(field, value)

        def install_conflicting_head(data):
            retained = data[head_path]
            data[head_path] = self.module._build_thread_head_document(
                thread_id="thread-ledger",
                canonical_source_id="source-attacker",
                owner_data=data[owner_path],
                generation=retained["activeGeneration"],
                state="active",
                revision=retained["threadHeadRevision"],
                now=retained["updatedAt"],
                created_at=retained["createdAt"],
            )

        cases = (
            ("missing_identity", remove(identity_path)),
            (
                "malformed_identity",
                replace_field(identity_path, "threadId", None),
            ),
            ("missing_classification", remove(classification_path)),
            (
                "conflicting_snapshot",
                replace_field(
                    classification_path,
                    "snapshotImmutableHash",
                    "0" * 64,
                ),
            ),
            ("missing_explicit_owner", remove(owner_path)),
            (
                "nonmatching_explicit_owner",
                replace_field(owner_path, "ownerDecisionHash", "0" * 64),
            ),
            ("missing_ledger", remove(ledger_path)),
            (
                "unreadable_ledger",
                replace_field(ledger_path, "ledgerHash", "0" * 64),
            ),
            ("missing_admission", remove(self.admission_path)),
            (
                "nonsettleable_admission",
                replace_field(self.admission_path, "admissionState", "pending"),
            ),
            ("conflicting_thread_head", install_conflicting_head),
        )
        for case, corrupt in cases:
            with self.subTest(case=case):
                self.fake.data.clear()
                self.fake.data.update(deepcopy(baseline))
                corrupt(self.fake.data)
                before = deepcopy(self.fake.data)
                self.fake.events.clear()

                with self.assertRaises(self.module.SourceCoordinatorError):
                    self.settle()

                self.assertEqual(before, self.fake.data)
                self.assertNotIn(settlement_path, self.fake.data)
                self.assertEqual([], self.write_events())

    def test_marker_fail_before_apply_is_retryable_and_writes_nothing(self):
        self.settle_all_work()
        before = deepcopy(self.fake.data)
        self.fake.events.clear()
        self.fake.fail_next_commit = RuntimeError("marker commit unavailable")

        with self.assertRaises(self.module.SourceCoordinatorRetryable):
            self.settle()

        self.assertEqual(before, self.fake.data)
        self.assertEqual([], self.write_events())
        settlement = self.settle()
        self.assertEqual(
            settlement.settlement_hash,
            self.fake.data[
                f"users/user-1/sourceSettlements/{self.source_id}"
            ]["settlementHash"],
        )

    def test_ordinary_only_none_owner_settles_without_thread_head(self):
        fake = FakeFirestore()
        coordinator = self.module.SourceCoordinator(
            fake,
            uuid_factory=SequentialUUIDs(),
            now_factory=MutableClock(FROZEN_NOW),
        )
        identity = coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={"id": "graph-ordinary-only"},
            evidence_kind="graph_hydration",
            thread_id="thread-ordinary-only",
        )
        source_id = identity.canonical_source_id
        coordinator.classify_source_once(
            user_id="user-1",
            canonical_source_id=source_id,
            lease_seconds=60,
            classification_input=deepcopy(CLASSIFICATION_INPUT),
            classifier=lambda: (
                {
                    "schemaVersion": 1,
                    "transitionCandidates": [],
                    "ordinaryObligations": [
                        {
                            "type": "field_update",
                            "field": "stage",
                            "value": "warm",
                        }
                    ],
                },
                deepcopy(MODEL_PROPOSAL_EVIDENCE),
            ),
        )
        owner = coordinator.elect_transition_owner_from_snapshot(
            user_id="user-1",
            canonical_source_id=source_id,
        )
        self.assertEqual("none", owner["ownerKind"])
        ledger = coordinator.create_or_verify_source_work_ledger(
            user_id="user-1",
            canonical_source_id=source_id,
        )
        entry = ledger["entries"][0]
        coordinator.admit_pending_inbound(
            user_id="user-1",
            canonical_source_id=source_id,
            received_at=FROZEN_NOW,
            sent_at=FROZEN_NOW,
            saved_history_binding={
                "schemaVersion": 1,
                "historyKey": "history-ordinary-only",
            },
            index_binding={
                "schemaVersion": 1,
                "indexKey": "index-ordinary-only",
            },
        )
        work_arguments = {
            "user_id": "user-1",
            "canonical_source_id": source_id,
            "ledger_hash": ledger["ledgerHash"],
            "work_key": entry["workKey"],
            "payload_hash": entry["payloadHash"],
        }
        coordinator.record_source_work_applying(**work_arguments)
        coordinator.complete_source_work_entry(
            **work_arguments,
            completion_record={
                "schemaVersion": 1,
                "evidenceKind": "work_completion",
                "workKind": "field_update",
                "resultHash": "f" * 64,
            },
        )
        head_path = (
            "users/user-1/threadTransitionHeads/thread-ordinary-only"
        )
        self.assertNotIn(head_path, fake.data)

        settlement = coordinator.settle_source_markers_if_ready(
            user_id="user-1",
            canonical_source_id=source_id,
            ledger_hash=ledger["ledgerHash"],
        )

        self.assertNotIn(head_path, fake.data)
        self.assertEqual(1, settlement.alias_projection_count)
        self.assertEqual(
            "settled",
            fake.data[
                f"users/user-1/inboundPendingAdmissions/{source_id}"
            ]["admissionState"],
        )

    def test_fully_ready_source_creates_settlement_and_all_alias_projections(self):
        self.settle_all_work()
        self.fake.events.clear()

        settlement = self.settle()

        settlement_path = f"users/user-1/sourceSettlements/{self.source_id}"
        stored = self.fake.data[settlement_path]
        self.assertEqual(settlement.settlement_hash, stored["settlementHash"])
        self.assertEqual(2, len(stored["aliases"]))
        self.assertEqual("settled", self.fake.data[self.admission_path]["admissionState"])
        for descriptor in stored["aliases"]:
            projection_path = (
                "users/user-1/processedMessages/"
                f"{descriptor['sourceAliasKey']}"
            )
            projection = self.fake.data[projection_path]
            self.assertEqual(self.source_id, projection["canonicalSourceId"])
            self.assertEqual(stored["settlementHash"], projection["settlementHash"])
        writes = list(self.write_events())
        retry = self.settle()
        self.assertEqual(settlement, retry)
        self.assertEqual(writes, self.write_events())

    def test_tampered_deferred_or_dominance_evidence_blocks_markers(self):
        self.settle_all_work()
        delegated = self.stored_entry("property_unavailable")
        deferred_path = (
            "users/user-1/sourceDeferredWork/"
            f"{delegated['workKey']}"
        )
        self.fake.data[deferred_path]["bindingHash"] = "0" * 64
        before = deepcopy(self.fake.data)
        self.fake.events.clear()

        with self.assertRaises(self.module.SourceCoordinatorError):
            self.settle()

        self.assertEqual(before, self.fake.data)
        self.assertEqual([], self.write_events())

    def test_missing_or_differently_owned_alias_authority_blocks_settlement(self):
        self.settle_all_work()
        identity_path = f"users/user-1/sourceIdentities/{self.source_id}"
        alias_key = self.fake.data[identity_path]["verifiedAliases"][0][
            "sourceAliasKey"
        ]
        alias_path = f"users/user-1/sourceAliases/{alias_key}"
        baseline = deepcopy(self.fake.data)

        for case in ("missing", "different_owner"):
            with self.subTest(case=case):
                self.fake.data.clear()
                self.fake.data.update(deepcopy(baseline))
                if case == "missing":
                    del self.fake.data[alias_path]
                else:
                    self.fake.data[alias_path]["canonicalSourceId"] = (
                        "source-attacker"
                    )
                before = deepcopy(self.fake.data)
                self.fake.events.clear()

                with self.assertRaises(self.module.SourceCoordinatorError):
                    self.settle()

                self.assertEqual(before, self.fake.data)
                self.assertEqual([], self.write_events())

    def test_naked_legacy_marker_never_authorizes_or_gets_overwritten(self):
        self.settle_all_work()
        identity_path = f"users/user-1/sourceIdentities/{self.source_id}"
        alias_key = self.fake.data[identity_path]["verifiedAliases"][0][
            "sourceAliasKey"
        ]
        marker_path = f"users/user-1/processedMessages/{alias_key}"
        naked_marker = {"processedAt": FROZEN_NOW}
        self.fake.data[marker_path] = deepcopy(naked_marker)
        self.fake.events.clear()

        with self.assertRaises(self.module.SourceSettlementConflict):
            self.settle()

        self.assertEqual(naked_marker, self.fake.data[marker_path])
        self.assertNotIn(
            f"users/user-1/sourceSettlements/{self.source_id}",
            self.fake.data,
        )
        self.assertEqual([], self.write_events())

    def test_late_alias_repair_adds_projection_without_mutating_settlement(self):
        self.settle_all_work()
        self.settle()
        settlement_path = f"users/user-1/sourceSettlements/{self.source_id}"
        original_settlement = deepcopy(self.fake.data[settlement_path])
        self.coordinator.admit_or_repair_source_identity(
            user_id="user-1",
            hydrated_message={
                "id": "graph-ledger-late",
                "internetMessageId": "<ledger@example.test>",
            },
            evidence_kind="graph_hydration",
            thread_id="thread-ledger",
        )
        identity_path = f"users/user-1/sourceIdentities/{self.source_id}"
        aliases = self.fake.data[identity_path]["verifiedAliases"]
        late_alias = next(
            descriptor
            for descriptor in aliases
            if descriptor not in original_settlement["aliases"]
        )
        late_projection_path = (
            "users/user-1/processedMessages/"
            f"{late_alias['sourceAliasKey']}"
        )
        late_owner_path = (
            "users/user-1/sourceAliases/"
            f"{late_alias['sourceAliasKey']}"
        )
        self.assertNotIn(late_projection_path, self.fake.data)
        retained_owner = deepcopy(self.fake.data[late_owner_path])
        self.fake.data[late_owner_path]["canonicalSourceId"] = "source-attacker"
        self.fake.events.clear()

        with self.assertRaises(self.module.SourceCoordinatorError):
            self.settle()

        self.assertEqual(original_settlement, self.fake.data[settlement_path])
        self.assertNotIn(late_projection_path, self.fake.data)
        self.assertEqual([], self.write_events())

        self.fake.data[late_owner_path] = retained_owner
        self.fake.events.clear()

        repaired = self.settle()

        self.assertEqual(original_settlement, self.fake.data[settlement_path])
        self.assertEqual(
            original_settlement["settlementHash"],
            repaired.settlement_hash,
        )
        self.assertIn(late_projection_path, self.fake.data)
        self.assertEqual(1, len(self.write_events()))

    def test_marker_partial_commit_blocks_cursor(self):
        self.settle_all_work()
        identity_path = f"users/user-1/sourceIdentities/{self.source_id}"
        hidden_alias_key = self.fake.data[identity_path]["verifiedAliases"][0][
            "sourceAliasKey"
        ]
        hidden_path = f"users/user-1/processedMessages/{hidden_alias_key}"
        original_snapshot = self.fake._snapshot

        def corrupting_snapshot(document_ref):
            snapshot = original_snapshot(document_ref)
            applied_unknown_commit = any(
                event[0] == "commit_raised_after_apply"
                for event in self.fake.events
            )
            if applied_unknown_commit and document_ref.path == hidden_path:
                return FakeDocumentSnapshot(document_ref, None)
            return snapshot

        self.fake._snapshot = corrupting_snapshot
        self.fake.apply_then_raise_next_commit = RuntimeError(
            "unknown marker commit"
        )
        self.fake.events.clear()

        with self.assertRaises(self.module.SourceSettlementConflict):
            self.settle()

        cursor_writes = [
            event
            for event in self.write_events()
            if "sync/inbox" in event[1] or "cursor" in event[1]
        ]
        self.assertEqual([], cursor_writes)


if __name__ == "__main__":
    unittest.main()
