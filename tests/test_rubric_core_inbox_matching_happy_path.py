import os
os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import unittest
from unittest.mock import patch

from email_automation import messaging


class _FakeDocSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _FakeCollection(self._store, f"{self._path}/{name}")

    def set(self, data, merge=False):
        if merge and self._path in self._store:
            self._store[self._path].update(data)
        else:
            self._store[self._path] = dict(data)

    def get(self):
        return _FakeDocSnapshot(self._store.get(self._path))


class _FakeCollection:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _FakeDocRef(self._store, f"{self._path}/{doc_id}")


class _FakeFirestore:
    """Minimal stateful Firestore double: per-document key/value store."""

    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _FakeCollection(self.store, name)


class CoreInboxMatchingHappyPathTest(unittest.TestCase):
    def test_reply_is_matched_to_its_thread_by_internet_message_id(self):
        """Happy path: the REAL index_message_id / lookup_thread_by_message_id
        pair round-trips a reply's internetMessageId to the thread that owns it.

        This is exactly what scan_inbox_against_index relies on to route an
        inbound reply to its existing conversation thread.
        """
        fake_fs = _FakeFirestore()
        user_id = "user-123"
        thread_id = "thread-42"
        # An inbound reply as it arrives from Graph, with the canonical header form.
        reply_internet_message_id = "<CY4PR01MB-broker-reply-0001@example.com>"

        with patch.object(messaging, "_fs", fake_fs):
            # Real production write: index the message id -> thread mapping.
            indexed = messaging.index_message_id(
                user_id, reply_internet_message_id, thread_id
            )
            self.assertTrue(indexed, "index_message_id should report success")

            # Real production read: a later reply-scan resolves the same header
            # back to the owning thread with no other state.
            resolved = messaging.lookup_thread_by_message_id(
                user_id, reply_internet_message_id
            )

        self.assertEqual(
            resolved,
            thread_id,
            "reply must be matched to its originating thread by internetMessageId",
        )

        # Angle-bracket normalization: the same reply id without header brackets
        # (as some Graph payloads present it) resolves to the same thread,
        # proving the match is on the normalized internetMessageId, not the raw string.
        with patch.object(messaging, "_fs", fake_fs):
            resolved_unbracketed = messaging.lookup_thread_by_message_id(
                user_id, "CY4PR01MB-broker-reply-0001@example.com"
            )
        self.assertEqual(resolved_unbracketed, thread_id)

        # Negative control: an unrelated reply id is NOT matched to the thread,
        # so the happy-path match is discriminating, not a constant.
        with patch.object(messaging, "_fs", fake_fs):
            unrelated = messaging.lookup_thread_by_message_id(
                user_id, "<unrelated-message-9999@example.com>"
            )
        self.assertIsNone(unrelated)


if __name__ == "__main__":
    unittest.main()
