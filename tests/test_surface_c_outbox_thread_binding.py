"""Surface C hardening: dashboard outbox thread-reply binding validation.

The outbox document is written entirely client-side (InlineReplyComposer.jsx
:555-575): threadId, replyToMessageId, clientId and resumeThreadOnSend all
arrive unvalidated. These tests pin the backend send pipeline
(_send_single_outbox_item / _finalize_successful_outbox_item) to:

  1. never send a thread reply whose thread is missing, terminal
     (stopped/completed/closed), or owned by a different client;
  2. never send a thread reply whose replyToMessageId is not a message
     recorded under that thread (no silent conversion to a new send);
  3. re-resolve row_number from the confirmed thread, not the client payload;
  4. never flip a non-paused/non-active (terminal) or cross-client thread
     back to active on resumeThreadOnSend.
"""

import unittest
import os
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import email as email_module


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return self._data


class FakeQuery:
    def __init__(self, results):
        self._results = results

    def limit(self, _n):
        return self

    def stream(self):
        return list(self._results)


class FakeFirestoreNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def _key(self):
        return "/".join(self.path)

    def collection(self, name):
        return FakeFirestoreNode(self.root, self.path + [name])

    def document(self, name):
        return FakeFirestoreNode(self.root, self.path + [name])

    def get(self):
        return self.root.snapshots.get(self._key(), FakeSnapshot({}, exists=False))

    def set(self, data, merge=False):
        self.root.set_calls.append((self._key(), data, merge))

    def add(self, data):
        self.root.add_calls.append((self._key(), data))
        return FakeFirestoreNode(self.root, self.path + ["auto-id"])

    def delete(self):
        self.root.deleted_paths.append(self._key())

    def where(self, field, op, value):
        return FakeQuery(self.root.query_results.get((self._key(), field, op, value), []))


class FakeFirestore:
    def __init__(self, snapshots=None):
        self.snapshots = dict(snapshots or {})
        self.query_results = {}
        self.set_calls = []
        self.add_calls = []
        self.deleted_paths = []

    def collection(self, name):
        return FakeFirestoreNode(self, [name])


class FakeDocRef:
    def __init__(self, doc_id="outbox-1"):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []

    def delete(self):
        self.deleted = True

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))


class FakeDoc:
    def __init__(self, data, doc_id="outbox-1"):
        self.id = doc_id
        self.reference = FakeDocRef(doc_id)
        self._data = data

    def to_dict(self):
        return self._data


THREAD_KEY = "users/uid-1/threads/thread-1"
MESSAGE_KEY = "users/uid-1/threads/thread-1/messages/graph-message-1"
DEAD_LETTER_KEY = "users/uid-1/deadLetterQueue"


def _dashboard_reply_doc(overrides=None, doc_id="outbox-thread-reply"):
    """Mirror of what InlineReplyComposer.handleSend writes client-side."""
    data = {
        "assignedEmails": ["broker@example.invalid"],
        "script": "Hi Ron,\n\nCan you share the current details?\n\nThanks",
        "clientId": "client-a",
        "subject": "RE: 123 Main St, Henderson",
        "threadId": "thread-1",
        "replyToMessageId": "graph-message-1",
        "resumeThreadOnSend": True,
        "scriptSelectionMode": "exact",
        "forceScript": True,
        "isPersonalized": True,
        "attempts": 0,
        "rowNumber": 999,
        "followUpConfig": {"enabled": False},
    }
    if overrides:
        data.update(overrides)
    return FakeDoc(data, doc_id=doc_id)


def _dead_letter_reasons(fake_fs):
    return [
        payload.get("failureReason")
        for key, payload in fake_fs.add_calls
        if key == DEAD_LETTER_KEY
    ]


def _thread_status_sets(fake_fs):
    return [payload for key, payload, _merge in fake_fs.set_calls if key == THREAD_KEY]


class OutboxThreadReplyBindingTests(unittest.TestCase):
    """Gap 1 + 2: pre-send validation of client-supplied thread binding."""

    def _run_send(self, doc, fake_fs, reply_sender="me@example.invalid"):
        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value=reply_sender) as get_reply_sender, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email, \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row") as highlight_row:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )
        return get_reply_sender, send_outbox_as_reply, send_and_index_email, highlight_row

    def test_terminal_thread_reply_dead_letters_instead_of_sending(self):
        """Stopped thread must not receive a reply nor be resurrected."""
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "stopped", "rowNumber": 42}),
            MESSAGE_KEY: FakeSnapshot({"direction": "inbound"}),
        })
        doc = _dashboard_reply_doc()

        _get_reply_sender, send_outbox_as_reply, send_and_index_email, _highlight = \
            self._run_send(doc, fake_fs)

        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        reasons = _dead_letter_reasons(fake_fs)
        self.assertEqual(1, len(reasons))
        self.assertIn("thread_no_longer_open", reasons[0])
        # The stopped thread must never be flipped back to active.
        for payload in _thread_status_sets(fake_fs):
            self.assertNotEqual("active", payload.get("status"))

    def test_completed_thread_reply_dead_letters_instead_of_sending(self):
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "completed"}),
            MESSAGE_KEY: FakeSnapshot({"direction": "inbound"}),
        })
        doc = _dashboard_reply_doc()

        _get_reply_sender, send_outbox_as_reply, send_and_index_email, _highlight = \
            self._run_send(doc, fake_fs)

        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertIn("thread_no_longer_open", _dead_letter_reasons(fake_fs)[0])

    def test_cross_client_thread_reply_dead_letters_instead_of_sending(self):
        """Outbox doc carrying a threadId of a DIFFERENT client must be blocked."""
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-b", "status": "paused"}),
            MESSAGE_KEY: FakeSnapshot({"direction": "inbound"}),
        })
        doc = _dashboard_reply_doc()  # clientId: client-a

        _get_reply_sender, send_outbox_as_reply, send_and_index_email, _highlight = \
            self._run_send(doc, fake_fs)

        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertIn("thread_client_mismatch", _dead_letter_reasons(fake_fs)[0])
        for payload in _thread_status_sets(fake_fs):
            self.assertNotEqual("active", payload.get("status"))

    def test_missing_thread_reply_dead_letters_instead_of_sending(self):
        fake_fs = FakeFirestore()  # thread does not exist
        doc = _dashboard_reply_doc()

        _get_reply_sender, send_outbox_as_reply, send_and_index_email, _highlight = \
            self._run_send(doc, fake_fs)

        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertIn("thread_not_found", _dead_letter_reasons(fake_fs)[0])

    def test_unrecorded_reply_target_dead_letters_instead_of_new_send(self):
        """replyToMessageId not recorded under the thread must dead-letter, not
        silently convert into a brand-new indexed send to client-supplied emails."""
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "paused"}),
            # no MESSAGE_KEY snapshot, no query match
        })
        doc = _dashboard_reply_doc()

        # Reply sender mismatch would previously trigger the new-send fallback.
        _get_reply_sender, send_outbox_as_reply, send_and_index_email, _highlight = \
            self._run_send(doc, fake_fs, reply_sender="someoneelse@example.invalid")

        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertIn("reply_target_not_in_thread", _dead_letter_reasons(fake_fs)[0])

    def test_reply_target_matched_by_graph_message_id_query_is_allowed(self):
        """Messages keyed by internetMessageId are matched via sourceMessage.graphMessageId."""
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "paused"}),
        })
        fake_fs.query_results[(
            "users/uid-1/threads/thread-1/messages",
            "sourceMessage.graphMessageId",
            "==",
            "graph-message-1",
        )] = [FakeSnapshot({"direction": "inbound"})]
        doc = _dashboard_reply_doc()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="broker@example.invalid"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-reply-1",
                 "internetMessageId": "<graph-reply-1@example.invalid>",
             }) as send_outbox_as_reply, \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_outbox_as_reply.assert_called_once()
        self.assertEqual([], _dead_letter_reasons(fake_fs))
        self.assertTrue(doc.reference.deleted)

    def test_open_thread_reply_sends_and_re_resolves_row_from_thread(self):
        """Happy path: paused thread of the same client sends via Graph reply,
        row_number comes from the confirmed thread (42), not the client payload (999),
        and the paused thread is resumed after the send."""
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "paused", "rowNumber": 42}),
            MESSAGE_KEY: FakeSnapshot({"direction": "inbound"}),
        })
        doc = _dashboard_reply_doc()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender", return_value="broker@example.invalid"), \
             patch.object(email_module, "_send_outbox_as_reply", return_value={
                 "sent": True,
                 "error": None,
                 "sentMessageId": "graph-reply-1",
                 "internetMessageId": "<graph-reply-1@example.invalid>",
             }) as send_outbox_as_reply, \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "highlight_row") as highlight_row:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_outbox_as_reply.assert_called_once()
        self.assertTrue(doc.reference.deleted)
        self.assertEqual([], _dead_letter_reasons(fake_fs))
        highlight_row.assert_called_once_with("sheet-1", 42)
        resumed = [p for p in _thread_status_sets(fake_fs) if p.get("status") == "active"]
        self.assertEqual(1, len(resumed))
        self.assertEqual("waiting", resumed[0].get("followUpStatus"))


class FinalizeResumeGateTests(unittest.TestCase):
    """Gap 1: the resumeThreadOnSend merge must never resurrect a terminal or
    cross-client thread."""

    def _finalize(self, fake_fs, data_overrides=None):
        outbox_ref = FakeDocRef()
        data = {
            "clientId": "client-a",
            "threadId": "thread-1",
            "resumeThreadOnSend": True,
        }
        data.update(data_overrides or {})
        with patch("email_automation.clients._fs", fake_fs):
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                data,
            )
        return outbox_ref

    def test_finalize_does_not_resurrect_stopped_thread(self):
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "stopped"}),
        })
        outbox_ref = self._finalize(fake_fs)

        self.assertTrue(outbox_ref.deleted)  # outbox cleanup still happens
        self.assertEqual([], _thread_status_sets(fake_fs))

    def test_finalize_does_not_resurrect_completed_thread(self):
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "completed"}),
        })
        self._finalize(fake_fs)
        self.assertEqual([], _thread_status_sets(fake_fs))

    def test_finalize_does_not_touch_missing_thread(self):
        fake_fs = FakeFirestore()
        self._finalize(fake_fs)
        self.assertEqual([], _thread_status_sets(fake_fs))

    def test_finalize_does_not_flip_other_clients_thread(self):
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-b", "status": "paused"}),
        })
        self._finalize(fake_fs)  # outbox clientId is client-a
        self.assertEqual([], _thread_status_sets(fake_fs))

    def test_finalize_resumes_paused_thread_of_same_client(self):
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "paused"}),
        })
        outbox_ref = self._finalize(fake_fs)

        self.assertTrue(outbox_ref.deleted)
        resumed = _thread_status_sets(fake_fs)
        self.assertEqual(1, len(resumed))
        self.assertEqual("active", resumed[0]["status"])
        self.assertEqual("waiting", resumed[0]["followUpStatus"])

    def test_finalize_still_refreshes_active_thread_of_same_client(self):
        """Active thread keeps current legitimate behavior (followUpStatus refresh)."""
        fake_fs = FakeFirestore({
            THREAD_KEY: FakeSnapshot({"clientId": "client-a", "status": "active"}),
        })
        self._finalize(fake_fs)
        refreshed = _thread_status_sets(fake_fs)
        self.assertEqual(1, len(refreshed))
        self.assertEqual("active", refreshed[0]["status"])


if __name__ == "__main__":
    unittest.main()
