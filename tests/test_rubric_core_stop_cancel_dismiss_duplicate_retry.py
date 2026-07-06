import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation import email as email_module


class FakeDocRef:
    def __init__(self, doc_id="outbox-retry-1"):
        self.id = doc_id
        self.deleted = False

    def delete(self):
        self.deleted = True


class FakeDoc:
    def __init__(self, data, doc_id="outbox-retry-1"):
        self.id = doc_id
        self.reference = FakeDocRef(doc_id)
        self._data = data

    def to_dict(self):
        return self._data


class FakeFirestoreNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        return FakeFirestoreNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeFirestoreNode(self.root, self.path + ["document", name])

    def set(self, data, merge=False):
        self.root.set_calls.append((tuple(self.path), data, merge))

    def add(self, data):
        self.root.add_calls.append((tuple(self.path), data))
        return FakeFirestoreNode(self.root, self.path + ["document", "auto-id"])


class FakeFirestore:
    def __init__(self):
        self.set_calls = []
        self.add_calls = []

    def collection(self, name):
        return FakeFirestoreNode(self, ["collection", name])


class StopCancelDismissDuplicateRetryTest(unittest.TestCase):
    """core.stop_cancel_dismiss / duplicate_retry.

    Proves that a cancelled item is NOT re-sent when a subsequent worker run
    re-processes a stale-but-sendable queued snapshot. The enqueued snapshot
    looks fully sendable; only the pre-send live recheck reveals the cancel,
    and the real reconciliation guard must delete rather than re-send.
    """

    def _sendable_queued_snapshot(self):
        # This is the STALE snapshot captured on a prior scan/run. It carries a
        # real recipient + script and NO cancel markers, so a naive re-run would
        # send it. The dashboard-reply anchors force the individual send path.
        return {
            "assignedEmails": ["broker@example.com"],
            "script": "Hi Ron,\n\nFollowing up on the property.\n\nThanks",
            "clientId": "client-1",
            "notificationClientId": "client-1",
            "notificationId": "notification-1",
            "subject": "RE: 0 Gemini Ave, Houston",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "conversationId": "conversation-1",
            "rowNumber": 20,
            "actionAuditId": "audit-retry-1",
            "source": "dashboard_manual_reply",
            "followUpConfig": {"enabled": False},
        }

    def test_cancelled_item_is_not_resent_on_subsequent_run_presend_recheck(self):
        queued = self._sendable_queued_snapshot()
        doc = FakeDoc(queued, doc_id="outbox-retry-1")
        fake_fs = FakeFirestore()

        # Guard the premise: the stale queued snapshot is genuinely sendable.
        # If it were already cancelled at enqueue time this would prove the
        # wrong (terminal_state) guard rather than the subsequent-run recheck.
        self.assertFalse(email_module._is_cancelled_outbox_item(queued))

        # Live state on THIS subsequent run: the user cancelled between the
        # scan and the send (retry_after_uncertain_send / retry_reconciled).
        live_cancelled = {**queued, "cancelRequested": True, "status": "cancelled"}

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(
                 email_module, "_get_current_outbox_data", return_value=live_cancelled
             ), \
             patch.object(email_module, "_get_reply_message_sender") as get_reply_sender, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            # Exercise the REAL production function under test.
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": queued},
            )

        # No graph send happened on the subsequent run -> not re-sent.
        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()

        # The stale queued item was reconciled away instead of re-sent.
        self.assertTrue(doc.reference.deleted)

        # Terminal action-audit records the cancel, not a send.
        self.assertTrue(fake_fs.set_calls, "expected an action-audit write")
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("cancelled", audit_payload["status"])
        self.assertEqual("outbox-retry-1", audit_payload["outboxId"])


if __name__ == "__main__":
    unittest.main()
