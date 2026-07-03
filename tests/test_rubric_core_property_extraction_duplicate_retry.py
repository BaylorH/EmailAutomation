import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

import unittest
from unittest.mock import patch, MagicMock

from email_automation import messaging
from email_automation import processing


# ─────────────────────────────────────────────────────────────────────────────
# Minimal in-memory Firestore double.
#
# Only the *storage* backend is faked (patched into both ``messaging._fs`` and
# ``processing._fs`` as ONE shared store). The functions under test —
# ``processing.retry_processing_failures`` and the real
# ``messaging.has_processed`` / ``mark_processed`` idempotency guard it relies on
# — are exercised as genuine production code. ``process_inbox_message`` (the
# downstream unit that actually applies property/attachment extraction) is
# replaced by a call-counting spy so we can measure whether extraction is
# (re)applied, without running the whole AI pipeline.
#
# Path-tuple keyed store so both the nested
#   users/{uid}/processedMessages/{encodedKey}
# doc reads/writes and the
#   users/{uid}/processingFailures/{docId}
# collection scan work against the same data.
# ─────────────────────────────────────────────────────────────────────────────


class _Snapshot:
    def __init__(self, store, path):
        self._store = store
        self._path = path
        self.id = path[-1]

    @property
    def exists(self):
        return self._path in self._store.data

    def to_dict(self):
        return dict(self._store.data.get(self._path) or {})

    @property
    def reference(self):
        return _DocRef(self._store, self._path)


class _DocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def collection(self, name):
        return _CollectionRef(self._store, self._path + (name,))

    def get(self):
        return _Snapshot(self._store, self._path)

    def set(self, payload, merge=False):
        if merge and self._path in self._store.data:
            self._store.data[self._path].update(payload)
        else:
            self._store.data[self._path] = dict(payload)

    def delete(self):
        self._store.data.pop(self._path, None)


class _CollectionRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path

    def document(self, doc_id):
        return _DocRef(self._store, self._path + (doc_id,))

    def limit(self, n):
        return self

    def stream(self):
        depth = len(self._path) + 1
        for path in list(self._store.data.keys()):
            if len(path) == depth and path[: len(self._path)] == self._path:
                yield _Snapshot(self._store, path)


class _FakeFirestore:
    def __init__(self):
        self.data = {}

    def collection(self, name):
        return _CollectionRef(self, (name,))


class CorePropertyExtractionDuplicateRetryTests(unittest.TestCase):
    def test_reprocessing_same_reply_does_not_double_apply_extraction(self):
        """duplicate_retry: retry_processing_failures re-runs extraction
        (process_inbox_message) for a reply that was never processed, but the
        real has_processed guard skips a reply already marked processed — so the
        same reply is never extracted twice. The fresh reply is the negative
        control that proves the assertion is discriminating (extraction DOES
        happen when it should)."""
        fake_fs = _FakeFirestore()
        user_id = "uid-prop"

        already_id = "m-already-extracted"
        fresh_id = "m-fresh-reply"

        headers = {"Authorization": "Bearer test"}

        def fake_fetch(_headers, message_id):
            return {
                "id": message_id,
                "internetMessageId": message_id,
                "conversationId": f"conv-{message_id}",
            }

        with patch.object(messaging, "_fs", fake_fs), \
                patch.object(processing, "_fs", fake_fs), \
                patch.object(processing, "_fetch_graph_message_by_id", side_effect=fake_fetch), \
                patch.object(processing, "_find_existing_retry_artifact_for_message", return_value=None), \
                patch.object(processing, "_find_sent_item_continuing_conversation", return_value=None), \
                patch.object(processing, "process_inbox_message", MagicMock()) as spy_process:

            # Pre-state: the "already" reply was extracted on a prior pass and is
            # recorded processed via the REAL idempotency writer.
            messaging.mark_processed(user_id, already_id)
            self.assertTrue(messaging.has_processed(user_id, already_id))
            self.assertFalse(messaging.has_processed(user_id, fresh_id))

            # Both replies are sitting as retryable processing-failure records
            # (e.g. a transient error queued them for retry).
            failures = fake_fs.collection("users").document(user_id).collection("processingFailures")
            failures.document("f-already").set({
                "messageId": already_id,
                "threadId": "t-already",
                "clientId": "c1",
                "processingAttempts": 0,
                "retryable": True,
            })
            failures.document("f-fresh").set({
                "messageId": fresh_id,
                "threadId": "t-fresh",
                "clientId": "c1",
                "processingAttempts": 0,
                "retryable": True,
            })

            result = processing.retry_processing_failures(user_id, headers)

            # --- Core assertion: extraction is applied EXACTLY ONCE, and only
            # for the reply that had never been processed. The already-extracted
            # reply is NOT re-fed to process_inbox_message. ---
            self.assertEqual(spy_process.call_count, 1)
            processed_msg_ids = [c.args[2].get("id") for c in spy_process.call_args_list]
            self.assertIn(fresh_id, processed_msg_ids)
            self.assertNotIn(already_id, processed_msg_ids)

            # Retry bookkeeping: one skipped (already processed), one succeeded.
            self.assertEqual(result["checked"], 2)
            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(result["skipped"], 1)

            # The freshly-processed reply is now recorded processed by real code,
            # so a *subsequent* retry would also skip it (idempotent going forward).
            self.assertTrue(messaging.has_processed(user_id, fresh_id))

            # Negative-control cross-check: with the has_processed guard in play,
            # re-running the retry now re-extracts NOTHING (both are processed /
            # their failure docs were consumed).
            spy_process.reset_mock()
            result2 = processing.retry_processing_failures(user_id, headers)
            self.assertEqual(spy_process.call_count, 0)
            self.assertEqual(result2["succeeded"], 0)


if __name__ == "__main__":
    unittest.main()
