import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import main


class FakeDocRef:
    def __init__(self, doc_id, deleted_ids):
        self.doc_id = doc_id
        self.deleted_ids = deleted_ids

    def delete(self):
        self.deleted_ids.append(self.doc_id)


class FakeDoc:
    def __init__(self, doc_id, data, deleted_ids):
        self.id = doc_id
        self._data = data
        self.reference = FakeDocRef(doc_id, deleted_ids)

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
    def __init__(self):
        self.deleted_ids = []
        self.collections = {
            "processedMessages": FakeCollection([
                FakeDoc("processed-oldest", {"processedAt": 1}, self.deleted_ids),
                FakeDoc("processed-old", {"processedAt": 2}, self.deleted_ids),
                FakeDoc("processed-kept", {"processedAt": 3}, self.deleted_ids),
                FakeDoc("processed-newest", {"processedAt": 4}, self.deleted_ids),
            ]),
            "sheetChangeLog": FakeCollection([
                FakeDoc("change-oldest", {"timestamp": 1}, self.deleted_ids),
                FakeDoc("change-kept", {"timestamp": 2}, self.deleted_ids),
                FakeDoc("change-newest", {"timestamp": 3}, self.deleted_ids),
            ]),
        }

    def collection(self, name):
        return self

    def document(self, name):
        return self

    def collection(self, name):
        return self.collections[name] if name in self.collections else self


class CleanupRetentionTests(unittest.TestCase):
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

    def test_cleanup_timestamp_sort_handles_mixed_legacy_values(self):
        deleted_ids = []
        docs = [
            FakeDoc("missing-time", {}, deleted_ids),
            FakeDoc("iso-time", {"timestamp": "2026-06-05T08:00:00Z"}, deleted_ids),
            FakeDoc("datetime-time", {"timestamp": datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc)}, deleted_ids),
            FakeDoc("numeric-time", {"timestamp": 3}, deleted_ids),
        ]

        deleted = main._delete_oldest_excess_docs(FakeCollection(docs), 2, ["timestamp"])

        self.assertEqual(deleted, 2)
        self.assertEqual(deleted_ids, ["missing-time", "numeric-time"])


if __name__ == "__main__":
    unittest.main()
