import unittest
import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import messaging


class FakeDocSnapshot:
    def __init__(self, doc_id, data, collection):
        self.id = doc_id
        self._data = data
        self.reference = FakeMessageDocRef(collection, doc_id)

    def to_dict(self):
        return self._data


class FakeMessageDocRef:
    def __init__(self, collection, doc_id):
        self.collection_obj = collection
        self.id = doc_id

    def set(self, payload, merge=True):
        self.collection_obj.saved[self.id] = {"payload": payload, "merge": merge}

    def delete(self):
        self.collection_obj.deleted.append(self.id)


class FakeMessagesCollection:
    def __init__(self, existing):
        self.saved = {}
        self.deleted = []
        self._existing = [
            FakeDocSnapshot(doc_id, data, self)
            for doc_id, data in existing.items()
        ]

    def document(self, doc_id):
        return FakeMessageDocRef(self, doc_id)

    def stream(self):
        return list(self._existing)


class FakeChain:
    def __init__(self, messages):
        self.messages = messages

    def collection(self, name):
        if name == "messages":
            return self.messages
        return self

    def document(self, _doc_id):
        return self


class MessageHistoryDedupeTests(unittest.TestCase):
    def test_real_graph_outbound_replaces_matching_synthetic_dashboard_message(self):
        messages = FakeMessagesCollection({
            "dashboard-reply-123": {
                "direction": "outbound",
                "source": "dashboard_outbox_reply",
                "subject": "RE: 3660 N 5th St",
                "to": ["bp21harrison@gmail.com"],
                "bodyPreview": "The tenant is confidential for now. Please send anything in the 4,000-15,000 SF range.",
                "sentDateTime": "2026-05-06T18:00:49.028484+00:00",
            }
        })
        payload = {
            "direction": "outbound",
            "subject": "RE: 3660 N 5th St",
            "to": ["bp21harrison@gmail.com"],
            "body": {
                "preview": "The tenant is confidential for now. Please send anything in the 4,000-15,000 SF range.",
            },
            "sentDateTime": "2026-05-06T18:00:49Z",
            "headers": {"internetMessageId": "<real-message@example.com>"},
        }

        original_fs = messaging._fs
        messaging._fs = FakeChain(messages)
        try:
            self.assertTrue(messaging.save_message("uid-1", "thread-1", "real-message", payload))
        finally:
            messaging._fs = original_fs

        self.assertIn("dashboard-reply-123", messages.deleted)
        self.assertIn("real-message", messages.saved)

    def test_real_graph_outbound_keeps_unrelated_synthetic_message(self):
        messages = FakeMessagesCollection({
            "followup-thread-123": {
                "direction": "outbound",
                "source": "followup_scheduler",
                "subject": "RE: 3670 N 5th St",
                "to": ["baylor@manifoldengineering.ai"],
                "bodyPreview": "This is a different follow-up body.",
                "sentDateTime": "2026-05-06T17:50:16.731335+00:00",
            }
        })
        payload = {
            "direction": "outbound",
            "subject": "RE: 3670 N 5th St",
            "to": ["baylor@manifoldengineering.ai"],
            "body": {"preview": "Hi Test, I wanted to follow up on my previous email."},
            "sentDateTime": "2026-05-06T17:50:16Z",
        }

        original_fs = messaging._fs
        messaging._fs = FakeChain(messages)
        try:
            self.assertTrue(messaging.save_message("uid-1", "thread-1", "real-message", payload))
        finally:
            messaging._fs = original_fs

        self.assertEqual([], messages.deleted)

    def test_real_graph_followup_replaces_synthetic_when_only_reply_prefix_differs(self):
        messages = FakeMessagesCollection({
            "followup-thread-123": {
                "direction": "outbound",
                "source": "followup_scheduler",
                "subject": "3670 N 5th St, North Las Vegas",
                "to": ["baylor@manifoldengineering.ai"],
                "bodyPreview": "Hi Marcus, I wanted to follow up on my previous email regarding the property above.",
                "sentDateTime": "2026-05-26T09:06:31.200738+00:00",
            }
        })
        payload = {
            "direction": "outbound",
            "subject": "RE: 3670 N 5th St, North Las Vegas",
            "to": ["baylor@manifoldengineering.ai"],
            "body": {
                "preview": "Hi Marcus, I wanted to follow up on my previous email regarding the property above.",
            },
            "sentDateTime": "2026-05-26T09:06:31Z",
            "headers": {"internetMessageId": "<real-followup@example.com>"},
        }

        original_fs = messaging._fs
        messaging._fs = FakeChain(messages)
        try:
            self.assertTrue(messaging.save_message("uid-1", "thread-1", "real-message", payload))
        finally:
            messaging._fs = original_fs

        self.assertIn("followup-thread-123", messages.deleted)
        self.assertIn("real-message", messages.saved)


if __name__ == "__main__":
    unittest.main()
