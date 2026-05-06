import unittest
import os
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import email as email_module


class FakeDocRef:
    def __init__(self):
        self.deleted = False
        self.set_calls = []

    def delete(self):
        self.deleted = True

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))


class FakeDoc:
    def __init__(self, data):
        self.id = "outbox-1"
        self.reference = FakeDocRef()
        self._data = data

    def to_dict(self):
        return self._data


class OutboxReplyRecipientRoutingTests(unittest.TestCase):
    def _thread_reply_outbox(self, assigned_email):
        return FakeDoc({
            "assignedEmails": [assigned_email],
            "script": "Hi Casey,\n\nCould you confirm availability?\n\nThanks",
            "clientId": "client-1",
            "subject": "RE: 920 Wrong Contact Drive, Henderson",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "attempts": 0,
            "rowNumber": 12,
            "contactName": "Casey Broker",
        })

    @patch.object(email_module, "_claim_outbox_item", return_value=True)
    @patch.object(email_module, "_get_reply_message_sender", return_value="baylor@manifoldengineering.ai")
    @patch.object(email_module, "_send_outbox_as_reply")
    @patch.object(email_module, "send_and_index_email", return_value={"sent": ["casey.test@example.invalid"], "errors": {}})
    @patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1")
    @patch.object(email_module, "highlight_row")
    def test_thread_reply_with_different_assigned_email_sends_new_indexed_message(
        self,
        _highlight_row,
        _get_sheet_id_or_fail,
        send_and_index_email,
        send_outbox_as_reply,
        _get_reply_message_sender,
        _claim_outbox_item,
    ):
        doc = self._thread_reply_outbox("casey.test@example.invalid")

        email_module._send_single_outbox_item(
            "uid-1",
            {"Authorization": "Bearer token"},
            {"doc": doc, "data": doc.to_dict()},
        )

        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_called_once()
        args, kwargs = send_and_index_email.call_args
        self.assertEqual(args[0], "uid-1")
        self.assertEqual(args[3], ["casey.test@example.invalid"])
        self.assertEqual(kwargs["client_id_or_none"], "client-1")
        self.assertEqual(kwargs["subject_override"], "RE: 920 Wrong Contact Drive, Henderson")
        self.assertEqual(kwargs["contact_name"], "Casey Broker")
        self.assertTrue(doc.reference.deleted)

    @patch.object(email_module, "_claim_outbox_item", return_value=True)
    @patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com")
    @patch.object(email_module, "_send_outbox_as_reply", return_value={"sent": True, "error": None})
    @patch.object(email_module, "send_and_index_email")
    @patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1")
    @patch.object(email_module, "highlight_row")
    def test_thread_reply_with_same_assigned_email_uses_graph_reply(
        self,
        _highlight_row,
        _get_sheet_id_or_fail,
        send_and_index_email,
        send_outbox_as_reply,
        _get_reply_message_sender,
        _claim_outbox_item,
    ):
        doc = self._thread_reply_outbox("bp21harrison@gmail.com")

        email_module._send_single_outbox_item(
            "uid-1",
            {"Authorization": "Bearer token"},
            {"doc": doc, "data": doc.to_dict()},
        )

        send_and_index_email.assert_not_called()
        send_outbox_as_reply.assert_called_once()
        self.assertTrue(doc.reference.deleted)


if __name__ == "__main__":
    unittest.main()
