import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

import unittest
from unittest.mock import patch

from email_automation import messaging


# ─────────────────────────────────────────────────────────────────────────────
# Minimal in-memory Firestore double.
#
# Only the storage backend (`messaging._fs`) is faked. The functions under test
# — messaging.has_processed / mark_processed / _processed_ref / b64url_id — are
# the REAL production code and are exercised end to end against this store.
#
# Semantics that matter for idempotency:
#   * document ids are stable string keys (so the same message id always maps to
#     the same document)
#   * .set(payload, merge=True) upserts into the same doc rather than appending
#   * .get().exists reflects whether that exact doc was ever written
# ─────────────────────────────────────────────────────────────────────────────


class FakeSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeDocRef:
    def __init__(self, collection, doc_id):
        self._collection = collection
        self._id = doc_id

    def get(self):
        return FakeSnapshot(self._collection.docs.get(self._id))

    def set(self, payload, merge=False):
        if merge and self._id in self._collection.docs:
            self._collection.docs[self._id].update(payload)
        else:
            self._collection.docs[self._id] = dict(payload)

    def collection(self, name):
        return self._collection.store.collection_for(self._path_prefix() + f"/{name}")

    def _path_prefix(self):
        return f"{self._collection.path}/{self._id}"


class FakeCollection:
    def __init__(self, store, path):
        self.store = store
        self.path = path
        self.docs = {}

    def document(self, doc_id):
        return FakeDocRef(self, doc_id)


class FakeFirestore:
    def __init__(self):
        self._collections = {}

    def collection_for(self, path):
        if path not in self._collections:
            self._collections[path] = FakeCollection(self, path)
        return self._collections[path]

    def collection(self, name):
        return self.collection_for(name)

    def processed_docs(self, user_id):
        path = f"users/{user_id}/processedMessages"
        col = self._collections.get(path)
        return col.docs if col else {}


class CoreInboxMatchingDuplicateRetryTests(unittest.TestCase):
    """Rubric cell core.inbox_matching / duplicate_retry.

    Proves that reprocessing the *same* inbound message is idempotent: the
    real has_processed/mark_processed pair recognizes an already-seen message
    and records exactly one processed-marker, so a retry cannot double-act.
    """

    def test_duplicate_inbound_message_marks_processed_once_and_is_idempotent(self):
        fake_fs = FakeFirestore()
        user_id = "uid-dup-retry"
        message_key = "<inbound-message-1@broker.example.test>"
        other_key = "<inbound-message-2@broker.example.test>"

        with patch.object(messaging, "_fs", fake_fs):
            # 1. First delivery: message has never been processed.
            self.assertFalse(
                messaging.has_processed(user_id, message_key),
                "unseen message must not report as already processed",
            )

            # 2. Process it once → mark it processed (the real write path).
            messaging.mark_processed(user_id, message_key)

            # 3. The retry pass now sees it as processed and would skip re-acting.
            self.assertTrue(
                messaging.has_processed(user_id, message_key),
                "after mark_processed the same message must report processed",
            )

            # 4. Idempotency: reprocessing the identical message a second time
            #    must not create a second marker / double-save.
            messaging.mark_processed(user_id, message_key)
            self.assertTrue(messaging.has_processed(user_id, message_key))

            processed = fake_fs.processed_docs(user_id)
            self.assertEqual(
                1,
                len(processed),
                "duplicate processing of the same message must write exactly one "
                f"processed-marker, got {len(processed)}: {list(processed)}",
            )

            # 5. The marker is keyed on the message id (via real b64url_id), so a
            #    genuinely different inbound message is unaffected — the guard is
            #    per-message, not a blanket suppression.
            self.assertFalse(messaging.has_processed(user_id, other_key))


if __name__ == "__main__":
    unittest.main()
