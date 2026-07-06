import os
os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation import messaging


# --- Minimal Firestore double honoring the exact API + dotted-field merge
# semantics that messaging.mark_event_handled / get_handled_events rely on. ---
class _FakeDoc:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _FakeCollectionRef(self._store, self._path + (name,))

    def get(self):
        return _FakeDoc(self._store.get(self._path))

    def set(self, data, merge=False):
        existing = self._store.get(self._path)
        if existing is None or not merge:
            existing = {}
        else:
            existing = dict(existing)
        for key, value in data.items():
            if "." in key:
                # Real Firestore treats a dotted key as a nested field path,
                # merging into the parent map without clobbering siblings.
                head, tail = key.split(".", 1)
                nested = dict(existing.get(head) or {})
                nested[tail] = value
                existing[head] = nested
            else:
                existing[key] = value
        self._store[self._path] = existing


class _FakeCollectionRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _FakeDocRef(self._store, self._path + (doc_id,))


class _FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _FakeCollectionRef(self.store, (name,))


class CoreEventClassifierDuplicateRetryTests(unittest.TestCase):
    def test_same_event_is_not_handled_twice_across_retry(self):
        """duplicate_retry: a re-detected identical event dedupes to the same
        key and is only marked/handled once, so a retry does not re-fire it."""
        fake_fs = _FakeFirestore()
        user_id = "uid-dup"
        thread_id = "thread-dup"

        # A "call_requested" event re-surfaced on two separate processing passes.
        # The second pass carries extra/noisy fields, but it is the same logical
        # event on the same thread -> must produce the identical dedup key.
        event_first_pass = {"reason": "wants a call"}
        event_retry_pass = {"reason": "wants a call", "extra": "noise", "ts": 123}

        with patch.object(messaging, "_fs", fake_fs):
            key1 = messaging.build_event_key("call_requested", event_first_pass, thread_id)
            key2 = messaging.build_event_key("call_requested", event_retry_pass, thread_id)

            # Stable, thread-unique key regardless of incidental payload fields.
            self.assertEqual(key1, key2)
            self.assertEqual(key1, "call_requested")

            # First pass: not yet handled -> real classifier records it once.
            self.assertFalse(messaging.is_event_handled(user_id, thread_id, key1))
            self.assertTrue(
                messaging.mark_event_handled(
                    user_id, thread_id, key1, msg_id="m1", notif_id="n1"
                )
            )

            # Retry pass: the SAME event is now recognized as already handled,
            # so the pipeline would skip re-notifying.
            self.assertTrue(messaging.is_event_handled(user_id, thread_id, key2))

            # And a second mark of an unrelated event on the same thread must not
            # clobber the first (dotted-field merge preserves prior handledEvents).
            other_key = messaging.build_event_key(
                "needs_user_input", {"reason": "confidential"}, thread_id
            )
            self.assertFalse(messaging.is_event_handled(user_id, thread_id, other_key))
            messaging.mark_event_handled(user_id, thread_id, other_key)

            handled = messaging.get_handled_events(user_id, thread_id)
            # Exactly one record for the deduped event, and both coexist.
            self.assertIn(key1, handled)
            self.assertIn(other_key, handled)
            self.assertEqual(len(handled), 2)
            # The original detection metadata for the deduped event is intact,
            # i.e. it was written once and never overwritten by the retry.
            self.assertEqual(handled[key1].get("detectedInMessageId"), "m1")
            self.assertEqual(handled[key1].get("notificationId"), "n1")


if __name__ == "__main__":
    unittest.main()
