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
    def __init__(self, doc_id="outbox-wrongrcpt-1"):
        self.id = doc_id
        self.deleted = False

    def delete(self):
        self.deleted = True


class FakeDoc:
    def __init__(self, data, doc_id="outbox-wrongrcpt-1"):
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


class StopCancelDismissWrongRecipientTest(unittest.TestCase):
    """core.stop_cancel_dismiss / wrong_recipient.

    Proves that when a queued send is cancelled because its live recipient has
    changed to the WRONG address between scan and send, the real pre-send
    recheck in ``_send_single_outbox_item`` deletes the item and refuses to
    invoke any Graph send -- so the wrong/changed recipient is never emailed.
    A negative control (live recipient unchanged and NOT cancelled) shows the
    same code path DOES reach the send, making the gate discriminating rather
    than a vacuous "never sends".
    """

    CORRECT_RECIPIENT = "broker@example.com"
    WRONG_RECIPIENT = "wrong-new-broker@example.com"

    def _sendable_queued_snapshot(self):
        # Stale snapshot captured on a prior scan: correct recipient, real
        # script, NO cancel markers -> a naive re-run would send it. Thread
        # anchors force the individual dashboard-reply send path.
        return {
            "assignedEmails": [self.CORRECT_RECIPIENT],
            "script": "Hi Ron,\n\nFollowing up on the property.\n\nThanks",
            "clientId": "client-1",
            "notificationClientId": "client-1",
            "notificationId": "notification-1",
            "subject": "RE: 0 Gemini Ave, Houston",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "conversationId": "conversation-1",
            "rowNumber": 20,
            "actionAuditId": "audit-wrongrcpt-1",
            "source": "dashboard_manual_reply",
            "followUpConfig": {"enabled": False},
        }

    def _run_send(self, live_data, get_reply_sender_return):
        """Exercise the REAL _send_single_outbox_item; return (doc, fake_fs, mocks)."""
        queued = self._sendable_queued_snapshot()
        doc = FakeDoc(queued, doc_id="outbox-wrongrcpt-1")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value=live_data), \
             patch.object(email_module, "_pause_results_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_should_preflight_sent_items_retry", return_value=False), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={}), \
             patch.object(email_module, "_sent_retry_reconciliation_result", return_value={"sent": False}), \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_finalize_successful_outbox_item"), \
             patch.object(
                 email_module, "_get_reply_message_sender",
                 return_value=get_reply_sender_return,
             ) as get_reply_sender, \
             patch.object(
                 email_module, "_send_outbox_as_reply",
                 return_value={
                     "sent": True,
                     "sentMessageId": "m1",
                     "internetMessageId": "i1",
                     "conversationId": "conversation-1",
                     "toRecipients": [self.CORRECT_RECIPIENT],
                     "sentRecipients": [self.CORRECT_RECIPIENT],
                 },
             ) as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            try:
                email_module._send_single_outbox_item(
                    "uid-1",
                    {"Authorization": "Bearer token"},
                    {"doc": doc, "data": queued},
                )
            except Exception as exc:
                # Downstream success bookkeeping is patched to no-op, so the gate
                # decision (send blocked vs. send path entered) is fully exercised
                # without any residual error. A bare ``except Exception: pass`` here
                # would silently swallow a real regression in _send_single_outbox_item;
                # instead fail loudly with context so unexpected errors surface.
                self.fail(
                    "_send_single_outbox_item raised unexpectedly after the gate "
                    f"decision: {type(exc).__name__}: {exc}"
                )

        return doc, fake_fs, get_reply_sender, send_outbox_as_reply, send_and_index_email

    def test_cancelled_wrong_recipient_change_blocks_send_but_valid_recipient_sends(self):
        queued = self._sendable_queued_snapshot()

        # Premise guard: the stale queued snapshot is genuinely sendable and
        # targets the CORRECT recipient -- so we are proving the pre-send
        # recheck, not a pre-cancelled enqueue.
        self.assertFalse(email_module._is_cancelled_outbox_item(queued))
        self.assertEqual(queued["assignedEmails"], [self.CORRECT_RECIPIENT])

        # Live state on THIS run: operator caught that the recipient was changed
        # to the WRONG address and cancelled the queued send.
        cancelled_wrong = {
            **queued,
            "assignedEmails": [self.WRONG_RECIPIENT],
            "cancelRequested": True,
            "status": "cancelled",
        }
        self.assertTrue(email_module._is_cancelled_outbox_item(cancelled_wrong))

        doc, fake_fs, get_reply_sender, send_outbox_as_reply, send_and_index_email = (
            self._run_send(cancelled_wrong, get_reply_sender_return=self.WRONG_RECIPIENT)
        )

        # No Graph send of any kind -> the WRONG recipient is never emailed.
        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()

        # The cancelled item was reconciled away instead of sent.
        self.assertTrue(doc.reference.deleted)
        self.assertTrue(fake_fs.set_calls, "expected a terminal action-audit write")
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("cancelled", audit_payload["status"])
        self.assertEqual("outbox-wrongrcpt-1", audit_payload["outboxId"])

        # NEGATIVE CONTROL: identical stale snapshot, but live state is unchanged
        # (correct recipient, NOT cancelled). The SAME real function must now
        # reach the send path -- proving the block above is caused by the
        # cancel/wrong-recipient change, not by the harness never sending.
        valid_live = dict(queued)
        self.assertFalse(email_module._is_cancelled_outbox_item(valid_live))

        doc2, _fs2, get_reply_sender2, send_outbox_as_reply2, _sie2 = self._run_send(
            valid_live, get_reply_sender_return=self.CORRECT_RECIPIENT
        )

        # The valid, uncancelled item is NOT deleted by the cancel gate and the
        # send path is entered (recipient source resolved + graph reply issued).
        self.assertFalse(doc2.reference.deleted)
        get_reply_sender2.assert_called_once()
        send_outbox_as_reply2.assert_called_once()


if __name__ == "__main__":
    unittest.main()
