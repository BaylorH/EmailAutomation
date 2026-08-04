import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import main
from scripts import production_reset


class FakeDocRef:
    def __init__(self, collection_name, doc_id, deleted_ids, deleted_paths):
        self.collection_name = collection_name
        self.doc_id = doc_id
        self.deleted_ids = deleted_ids
        self.deleted_paths = deleted_paths

    def delete(self):
        self.deleted_ids.append(self.doc_id)
        self.deleted_paths.append(f"{self.collection_name}/{self.doc_id}")


class FakeDoc:
    def __init__(
        self,
        collection_name,
        doc_id,
        data,
        deleted_ids,
        deleted_paths,
    ):
        self.id = doc_id
        self._data = data
        self.reference = FakeDocRef(
            collection_name,
            doc_id,
            deleted_ids,
            deleted_paths,
        )

    def to_dict(self):
        return self._data


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self._limit = None

    def limit(self, count):
        clone = FakeCollection(self.docs)
        clone._limit = count
        return clone

    def stream(self):
        if self._limit is None:
            return list(self.docs)
        return list(self.docs[:self._limit])


class FakeFirestore:
    B1_AUTHORITY_COLLECTIONS = (
        "sourceIdentities",
        "sourceAliases",
        "sourceClassifications",
        "sourceTransitionOwners",
        "threadTransitionHeads",
        "sourceWorkLedgers",
        "sourceDeferredWork",
        "inboundPendingAdmissions",
        "blockedSources",
        "sourceSettlements",
    )

    def __init__(self, *, processed_rows=None, changelog_rows=None):
        self.deleted_ids = []
        self.deleted_paths = []
        self.accessed_collections = []
        if processed_rows is None:
            processed_rows = [
                ("processed-oldest", {"processedAt": 1}),
                ("processed-old", {"processedAt": 2}),
                ("processed-kept", {"processedAt": 3}),
                ("processed-newest", {"processedAt": 4}),
            ]
        if changelog_rows is None:
            changelog_rows = [
                ("change-oldest", {"timestamp": 1}),
                ("change-kept", {"timestamp": 2}),
                ("change-newest", {"timestamp": 3}),
            ]
        self.collections = {
            "processedMessages": FakeCollection(
                self._docs("processedMessages", processed_rows)
            ),
            "sheetChangeLog": FakeCollection(
                self._docs("sheetChangeLog", changelog_rows)
            ),
        }
        for collection_name in self.B1_AUTHORITY_COLLECTIONS:
            self.collections[collection_name] = FakeCollection(
                self._docs(
                    collection_name,
                    [(f"{collection_name}-authority", {"createdAt": 0})],
                )
            )

    def _docs(self, collection_name, rows):
        return [
            FakeDoc(
                collection_name,
                doc_id,
                data,
                self.deleted_ids,
                self.deleted_paths,
            )
            for doc_id, data in rows
        ]

    def document(self, name):
        return self

    def collection(self, name):
        if name in self.collections:
            self.accessed_collections.append(name)
            return self.collections[name]
        return self


def _canonical_processed_projection(processed_at):
    return {
        "schemaVersion": 1,
        "sourceAliasKey": "a" * 64,
        "aliasType": "graph",
        "normalizedValueHash": "b" * 64,
        "canonicalSourceId": "source-0001",
        "settlementRevision": 1,
        "settlementHash": "c" * 64,
        "processedAt": processed_at,
    }


class CleanupRetentionTests(unittest.TestCase):
    def test_production_reset_includes_complete_b1_authority_graph(self):
        self.assertTrue(
            set(FakeFirestore.B1_AUTHORITY_COLLECTIONS).issubset(
                production_reset.COLLECTIONS_TO_WIPE
            )
        )

    def test_production_reset_dry_run_materializes_bounded_pages(self):
        stream_state = {
            "active": False,
            "interrupted": False,
            "calls": 0,
        }

        class DryRunDoc:
            class Reference:
                def __init__(self, path, state):
                    self._path = path
                    self._state = state

                @property
                def path(self):
                    if self._state["active"]:
                        self._state["interrupted"] = True
                    return self._path

            def __init__(self, path, state):
                self.reference = self.Reference(path, state)

        class FullPageCollection:
            def __init__(self, docs, state, *, start=0, limit=None):
                self.docs = docs
                self.state = state
                self.start = start
                self._limit = limit

            def order_by(self, field):
                self.assert_document_order(field)
                return FullPageCollection(
                    self.docs,
                    self.state,
                    start=self.start,
                    limit=self._limit,
                )

            @staticmethod
            def assert_document_order(field):
                if field != "__name__":
                    raise AssertionError(f"unexpected order field: {field}")

            def limit(self, count):
                return FullPageCollection(
                    self.docs,
                    self.state,
                    start=self.start,
                    limit=count,
                )

            def start_after(self, snapshot):
                return FullPageCollection(
                    self.docs,
                    self.state,
                    start=self.docs.index(snapshot) + 1,
                    limit=self._limit,
                )

            def stream(self):
                end = (
                    None
                    if self._limit is None
                    else self.start + self._limit
                )
                selected = self.docs[self.start:end]

                def snapshots():
                    self.state["calls"] += 1
                    self.state["active"] = True
                    self.state["interrupted"] = False
                    try:
                        for doc in selected:
                            if self.state["interrupted"]:
                                return
                            yield doc
                    finally:
                        self.state["active"] = False
                        self.state["interrupted"] = False

                return snapshots()

        docs = [
            DryRunDoc(path, stream_state)
            for path in ("one", "two", "three")
        ]
        collection = FullPageCollection(docs, stream_state)

        deleted = production_reset.delete_collection_batched(
            object(),
            collection,
            batch_size=2,
            dry_run=True,
        )

        self.assertEqual(3, deleted)
        self.assertEqual(2, stream_state["calls"])

    def test_production_reset_traverses_every_nested_parent(self):
        stream_state = {
            "active": False,
            "interrupted": False,
        }

        class NestedCollection:
            pass

        class ParentReference:
            def __init__(self, index):
                self.index = index

            def collection(self, _name):
                return NestedCollection()

        class ParentDoc:
            def __init__(self, index):
                self.index = index
                self.reference = ParentReference(index)

        class ParentCollection:
            def __init__(self, docs, state, *, start=0, limit=None):
                self.docs = docs
                self.state = state
                self.start = start
                self._limit = limit

            def order_by(self, field):
                if field != "__name__":
                    raise AssertionError(f"unexpected order field: {field}")
                return ParentCollection(
                    self.docs,
                    self.state,
                    start=self.start,
                    limit=self._limit,
                )

            def limit(self, count):
                return ParentCollection(
                    self.docs,
                    self.state,
                    start=self.start,
                    limit=count,
                )

            def start_after(self, snapshot):
                return ParentCollection(
                    self.docs,
                    self.state,
                    start=snapshot.index + 1,
                    limit=self._limit,
                )

            def stream(self):
                end = (
                    None
                    if self._limit is None
                    else self.start + self._limit
                )
                selected = self.docs[self.start:end]

                def snapshots():
                    self.state["active"] = True
                    self.state["interrupted"] = False
                    try:
                        for doc in selected:
                            if self.state["interrupted"]:
                                return
                            yield doc
                    finally:
                        self.state["active"] = False
                        self.state["interrupted"] = False

                return snapshots()

        class UserReference:
            def __init__(self, parents):
                self.parents = parents

            def collection(self, name):
                if name != "threads":
                    raise AssertionError(f"unexpected collection: {name}")
                return ParentCollection(self.parents, stream_state)

        class UsersCollection:
            def __init__(self, parents):
                self.parents = parents

            def document(self, _user_id):
                return UserReference(self.parents)

        class ResetFirestore:
            def __init__(self, parents):
                self.parents = parents

            def collection(self, name):
                if name != "users":
                    raise AssertionError(f"unexpected root collection: {name}")
                return UsersCollection(self.parents)

        parents = [ParentDoc(index) for index in range(501)]
        fake_db = ResetFirestore(parents)

        def delete_collection(_db, collection_ref, **_kwargs):
            if (
                isinstance(collection_ref, NestedCollection)
                and stream_state["active"]
            ):
                stream_state["interrupted"] = True
            return 1

        with patch.object(
            production_reset,
            "COLLECTIONS_TO_WIPE",
            ["threads"],
        ), patch.object(
            production_reset,
            "NESTED_COLLECTIONS",
            {"threads": ["messages"]},
        ), patch.object(
            production_reset,
            "delete_collection_batched",
            side_effect=delete_collection,
        ) as delete_collection:
            stats = production_reset.wipe_user_data(
                fake_db,
                "user-1",
                dry_run=True,
            )

        nested_calls = [
            call
            for call in delete_collection.call_args_list
            if isinstance(call.args[1], NestedCollection)
        ]
        self.assertEqual(501, len(nested_calls))
        self.assertEqual(501, stats["nested_deleted"])

    def test_auto_cleanup_deletes_only_oldest_excess_docs(self):
        fake_fs = FakeFirestore()

        with patch.object(main, "_fs", fake_fs), \
             patch.object(main, "PROCESSED_MESSAGES_THRESHOLD", 2), \
             patch.object(main, "SHEET_CHANGELOG_THRESHOLD", 2):
            main.auto_cleanup_firestore("uid-1")

        self.assertEqual(
            fake_fs.deleted_ids,
            ["processed-oldest", "processed-old", "change-oldest"],
        )

    def test_b1_projection_is_retained_without_consuming_legacy_quota(self):
        fake_fs = FakeFirestore(
            processed_rows=[
                ("b1-oldest", _canonical_processed_projection(0)),
                ("legacy-oldest", {"processedAt": 1}),
                ("legacy-kept", {"processedAt": 2}),
                ("legacy-newest", {"processedAt": 3}),
            ],
            changelog_rows=[],
        )

        with patch.object(main, "_fs", fake_fs), \
             patch.object(main, "PROCESSED_MESSAGES_THRESHOLD", 2), \
             patch.object(main, "SHEET_CHANGELOG_THRESHOLD", 2):
            main.auto_cleanup_firestore("uid-1")

        self.assertEqual(
            [
                path
                for path in fake_fs.deleted_paths
                if path.startswith("processedMessages/")
            ],
            ["processedMessages/legacy-oldest"],
        )
        self.assertNotIn("b1-oldest", fake_fs.deleted_ids)

    def test_b1_projections_alone_do_not_trigger_deletion(self):
        fake_fs = FakeFirestore(
            processed_rows=[
                (
                    f"b1-{index}",
                    {
                        **_canonical_processed_projection(index),
                        "sourceAliasKey": f"{index:064x}",
                    },
                )
                for index in range(3)
            ],
            changelog_rows=[],
        )

        with patch.object(main, "_fs", fake_fs), \
             patch.object(main, "PROCESSED_MESSAGES_THRESHOLD", 1), \
             patch.object(main, "SHEET_CHANGELOG_THRESHOLD", 1):
            main.auto_cleanup_firestore("uid-1")

        self.assertEqual([], fake_fs.deleted_paths)

    def test_partial_b1_ownership_markers_fail_closed(self):
        fake_fs = FakeFirestore(
            processed_rows=[
                (
                    "partial-canonical",
                    {"canonicalSourceId": "source-partial", "processedAt": 0},
                ),
                (
                    "partial-revision",
                    {"settlementRevision": 1, "processedAt": 0},
                ),
                (
                    "partial-settlement-hash",
                    {"settlementHash": "a" * 64, "processedAt": 0},
                ),
                (
                    "partial-alias-key",
                    {"sourceAliasKey": "b" * 64, "processedAt": 0},
                ),
                ("legacy-oldest", {"processedAt": 1}),
                ("legacy-kept", {"processedAt": 2}),
            ],
            changelog_rows=[],
        )

        with patch.object(main, "_fs", fake_fs), \
             patch.object(main, "PROCESSED_MESSAGES_THRESHOLD", 1), \
             patch.object(main, "SHEET_CHANGELOG_THRESHOLD", 1):
            main.auto_cleanup_firestore("uid-1")

        self.assertEqual(
            ["processedMessages/legacy-oldest"],
            fake_fs.deleted_paths,
        )

    def test_b1_authority_collections_are_never_accessed_or_deleted(self):
        fake_fs = FakeFirestore()

        with patch.object(main, "_fs", fake_fs), \
             patch.object(main, "PROCESSED_MESSAGES_THRESHOLD", 2), \
             patch.object(main, "SHEET_CHANGELOG_THRESHOLD", 2):
            main.auto_cleanup_firestore("uid-1")

        self.assertTrue(
            set(FakeFirestore.B1_AUTHORITY_COLLECTIONS).isdisjoint(
                fake_fs.accessed_collections
            )
        )
        self.assertFalse(
            any(
                path.split("/", 1)[0]
                in FakeFirestore.B1_AUTHORITY_COLLECTIONS
                for path in fake_fs.deleted_paths
            )
        )

    def test_cleanup_timestamp_sort_handles_mixed_legacy_values(self):
        deleted_ids = []
        deleted_paths = []
        docs = [
            FakeDoc("test", "missing-time", {}, deleted_ids, deleted_paths),
            FakeDoc(
                "test",
                "iso-time",
                {"timestamp": "2026-06-05T08:00:00Z"},
                deleted_ids,
                deleted_paths,
            ),
            FakeDoc(
                "test",
                "datetime-time",
                {
                    "timestamp": datetime(
                        2026,
                        6,
                        5,
                        9,
                        0,
                        tzinfo=timezone.utc,
                    )
                },
                deleted_ids,
                deleted_paths,
            ),
            FakeDoc(
                "test",
                "numeric-time",
                {"timestamp": 3},
                deleted_ids,
                deleted_paths,
            ),
        ]

        deleted = main._delete_oldest_excess_docs(FakeCollection(docs), 2, ["timestamp"])

        self.assertEqual(deleted, 2)
        self.assertEqual(deleted_ids, ["missing-time", "numeric-time"])


if __name__ == "__main__":
    unittest.main()
