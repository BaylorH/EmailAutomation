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


class FakeThreadDoc:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class FakeThreadQuery:
    def __init__(self, docs):
        self.docs = docs

    def stream(self):
        return [FakeThreadDoc(data) for data in self.docs]


class FakeThreadsCollection:
    def __init__(self, docs):
        self.docs = docs

    def where(self, *_args):
        return FakeThreadQuery(self.docs)


class FakeUserDoc:
    def __init__(self, docs):
        self.docs = docs

    def collection(self, name):
        self.assert_threads_collection(name)
        return FakeThreadsCollection(self.docs)

    def assert_threads_collection(self, name):
        if name != "threads":
            raise AssertionError(f"Unexpected collection: {name}")


class FakeUsersCollection:
    def __init__(self, docs):
        self.docs = docs

    def document(self, _uid):
        return FakeUserDoc(self.docs)


class FakeFirestoreForThreads:
    def __init__(self, docs):
        self.docs = docs

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected collection: {name}")
        return FakeUsersCollection(self.docs)


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

    def test_new_outreach_duplicate_check_is_scoped_to_client(self):
        existing_threads = [
            {
                "clientId": "older-client",
                "email": ["bp21harrison@gmail.com"],
                "subject": "2629 E Craig Rd, North Las Vegas",
            },
            {
                "clientId": "current-client",
                "email": ["bp21harrison@gmail.com"],
                "subject": "730 W Cheyenne Ave, North Las Vegas",
            },
        ]

        with patch("email_automation.clients._fs", FakeFirestoreForThreads(existing_threads)):
            blocked = email_module._has_existing_thread_for_property(
                "uid-1",
                "bp21harrison@gmail.com",
                "2629 E Craig Rd, North Las Vegas",
                client_id="current-client",
            )

        self.assertFalse(blocked)

    def test_new_outreach_duplicate_check_blocks_same_client_match(self):
        existing_threads = [
            {
                "clientId": "current-client",
                "email": ["bp21harrison@gmail.com"],
                "subject": "2629 E Craig Rd, North Las Vegas",
            },
        ]

        with patch("email_automation.clients._fs", FakeFirestoreForThreads(existing_threads)):
            blocked = email_module._has_existing_thread_for_property(
                "uid-1",
                "bp21harrison@gmail.com",
                "2629 E Craig Rd, North Las Vegas",
                client_id="current-client",
            )

        self.assertTrue(blocked)

    @patch.object(email_module, "_claim_outbox_item", return_value=True)
    @patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com")
    @patch.object(email_module, "_send_outbox_as_reply", return_value={"sent": True, "error": None})
    @patch.object(email_module, "_save_outbox_reply_message", create=True)
    @patch.object(email_module, "send_and_index_email")
    @patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1")
    @patch.object(email_module, "highlight_row")
    def test_thread_reply_with_same_assigned_email_uses_graph_reply(
        self,
        _highlight_row,
        _get_sheet_id_or_fail,
        send_and_index_email,
        save_outbox_reply_message,
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
        save_outbox_reply_message.assert_called_once()
        self.assertTrue(doc.reference.deleted)

    @patch.object(email_module, "_claim_outbox_item", return_value=True)
    @patch.object(email_module, "_get_thread_row_number", return_value=9, create=True)
    @patch.object(email_module, "_find_row_by_email", return_value=(3, []))
    @patch.object(email_module, "_get_reply_message_sender", return_value="bp21harrison@gmail.com")
    @patch.object(email_module, "_send_outbox_as_reply", return_value={"sent": True, "error": None})
    @patch.object(email_module, "_save_outbox_reply_message", create=True)
    @patch.object(email_module, "send_and_index_email")
    @patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1")
    @patch.object(email_module, "highlight_row")
    def test_thread_reply_without_row_number_uses_thread_row_before_email_lookup(
        self,
        highlight_row,
        _get_sheet_id_or_fail,
        send_and_index_email,
        _save_outbox_reply_message,
        _send_outbox_as_reply,
        _get_reply_message_sender,
        _find_row_by_email,
        _get_thread_row_number,
        _claim_outbox_item,
    ):
        doc = self._thread_reply_outbox("bp21harrison@gmail.com")
        data = doc.to_dict()
        data.pop("rowNumber")

        email_module._send_single_outbox_item(
            "uid-1",
            {"Authorization": "Bearer token"},
            {"doc": doc, "data": data},
        )

        _get_thread_row_number.assert_called_once_with("uid-1", "thread-1")
        _find_row_by_email.assert_not_called()
        highlight_row.assert_called_once_with("sheet-1", 9)
        send_and_index_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
