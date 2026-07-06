import os
os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from email_automation import processing


# --- Minimal Firestore double.
# Supports exactly the surface retry_processing_failures + its real retry-guard
# helpers touch: collection/document nesting, .limit(...).stream(), doc.to_dict,
# doc.reference.set(merge=)/delete(). It deliberately does NOT expose .where(),
# so the real duplicate-artifact scan helpers (which guard on `where`) treat
# every scanned collection as empty and fall through to the manual-continuation
# guard -- i.e. the production guard code runs for real, only the datastore is faked.
class _FakeDoc:
    def __init__(self, doc_id, data, ref):
        self.id = doc_id
        self._data = data
        self.reference = ref

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
        return _FakeCollection(self._store, self._path + (name,))

    def get(self):
        return _FakeDoc(self._path[-1], self._store.docs.get(self._path), self)

    def set(self, data, merge=False):
        cur = dict(self._store.docs.get(self._path) or {}) if merge else {}
        cur.update(data)
        self._store.docs[self._path] = cur

    def delete(self):
        self._store.docs.pop(self._path, None)
        self._store.deleted.append(self._path)


class _FakeCollection:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _FakeDocRef(self._store, self._path + (doc_id,))

    def limit(self, n):
        return self

    def stream(self):
        out = []
        for path, data in list(self._store.docs.items()):
            if len(path) == len(self._path) + 1 and path[:-1] == self._path:
                out.append(_FakeDoc(path[-1], data, _FakeDocRef(self._store, path)))
        return out


class _FakeFirestore:
    def __init__(self):
        self.docs = {}
        self.deleted = []

    def collection(self, name):
        return _FakeCollection(self, (name,))


USER_ID = "uid-manual-cont"
MESSAGE_ID = "orig-broker-msg-1"
THREAD_ID = "thread-manual-cont"
CLIENT_ID = "client-1"
CONVERSATION_ID = "AAConv-1"

GRAPH_MSG = {
    "id": "graph-msg-1",
    "conversationId": CONVERSATION_ID,
    "internetMessageId": "<inet-msg-1@contoso>",
}

SENT_CONTINUATION = {
    "collection": "SentItems",
    "id": "sent-reply-1",
    "internetMessageId": "<operator-reply-1@contoso>",
    "conversationId": CONVERSATION_ID,
    "sentDateTime": "2026-07-02T12:00:00Z",
}


def _seed_failure(fake_fs):
    """One retryable processing failure, exactly as the pipeline stores it."""
    path = ("users", USER_ID, "processingFailures", "fail-1")
    fake_fs.docs[path] = {
        "messageId": MESSAGE_ID,
        "threadId": THREAD_ID,
        "clientId": CLIENT_ID,
        "processingAttempts": 0,
        "retryable": True,
        "createdAt": datetime(2026, 7, 2, 11, 0, 0, tzinfo=timezone.utc),
    }
    return path


class CoreEventClassifierManualContinuationTests(unittest.TestCase):
    def test_manual_continuation_blocks_auto_reprocessing(self):
        """manual_continuation: when Sent Items shows the operator manually
        continued the thread after a processing failure, the real
        retry_processing_failures guard skips the failure and does NOT re-invoke
        the auto reply-processing/classifier path (process_inbox_message).

        The negative control -- identical inputs but no manual continuation --
        proves the guard, not the harness, is what suppresses the re-fire: there
        the same code path DOES run the classifier and clears the failure."""

        # ---- POSITIVE: operator manually continued -> auto-path must NOT re-fire.
        fake_fs = _FakeFirestore()
        fail_path = _seed_failure(fake_fs)
        auto_path_spy = MagicMock(name="process_inbox_message")

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "mark_processed"), \
             patch.object(processing, "_fetch_graph_message_by_id", return_value=dict(GRAPH_MSG)), \
             patch.object(processing, "find_sent_conversation_continuation_for_retry",
                          return_value=dict(SENT_CONTINUATION)) as guard_lookup, \
             patch.object(processing, "process_inbox_message", auto_path_spy):
            result_blocked = processing.retry_processing_failures(
                USER_ID, {"Authorization": "Bearer x"}, limit=10, max_attempts=3
            )

        # The real Sent-Items guard was consulted for the failed thread.
        self.assertTrue(guard_lookup.called)
        # The classifier / auto reply-processing path was NEVER re-run.
        auto_path_spy.assert_not_called()
        # The failure was counted as skipped, and nothing was reprocessed/succeeded.
        self.assertEqual(result_blocked["skipped"], 1)
        self.assertEqual(result_blocked["retried"], 0)
        self.assertEqual(result_blocked["succeeded"], 0)
        # The failure record survives (left visible for manual review) and is
        # marked blocked by manual continuation + made non-retryable.
        self.assertIn(fail_path, fake_fs.docs)
        blocked_doc = fake_fs.docs[fail_path]
        self.assertEqual(blocked_doc["recoveryStatus"], "blocked_manual_conversation_continued")
        self.assertFalse(blocked_doc["retryable"])

        # ---- NEGATIVE CONTROL: no manual continuation -> auto-path DOES fire.
        fake_fs2 = _FakeFirestore()
        fail_path2 = _seed_failure(fake_fs2)
        auto_path_spy2 = MagicMock(name="process_inbox_message")

        with patch.object(processing, "_fs", fake_fs2), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "mark_processed"), \
             patch.object(processing, "_fetch_graph_message_by_id", return_value=dict(GRAPH_MSG)), \
             patch.object(processing, "find_sent_conversation_continuation_for_retry",
                          return_value=None), \
             patch.object(processing, "process_inbox_message", auto_path_spy2):
            result_run = processing.retry_processing_failures(
                USER_ID, {"Authorization": "Bearer x"}, limit=10, max_attempts=3
            )

        # With no operator continuation, the SAME guarded path runs the classifier
        # exactly once and clears the (now-handled) failure.
        auto_path_spy2.assert_called_once()
        self.assertEqual(result_run["retried"], 1)
        self.assertEqual(result_run["succeeded"], 1)
        self.assertNotIn(fail_path2, fake_fs2.docs)
        self.assertIn(fail_path2, fake_fs2.deleted)


if __name__ == "__main__":
    unittest.main()
