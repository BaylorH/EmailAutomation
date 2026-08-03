import hashlib
import importlib
import importlib.util
import inspect
import json
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from tests.source_coordinator_fakes import (
    FakeDocumentSnapshot,
    FakeFirestore,
)


MODULE_NAME = "email_automation.source_coordinator"
FROZEN_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


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


if __name__ == "__main__":
    unittest.main()
