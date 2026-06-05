import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import pending_responses


class FakeDocRef:
    def __init__(self):
        self.deleted = False
        self.update_calls = []

    def delete(self):
        self.deleted = True

    def update(self, data):
        self.update_calls.append(data)


class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.reference = FakeDocRef()

    def to_dict(self):
        return self._data


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.add_calls = []

    def stream(self):
        return list(self.docs)

    def add(self, data):
        self.add_calls.append(data)
        return FakeDocRef()


class FakeFirestore:
    def __init__(self, pending_docs):
        self.collections = {
            "pendingResponses": FakeCollection(pending_docs),
            "deadLetterQueue": FakeCollection(),
        }

    def collection(self, name):
        return self

    def document(self, name):
        return self

    def collection(self, name):
        return self.collections.setdefault(name, FakeCollection()) if name != "users" else self


class PendingResponsesTests(unittest.TestCase):
    def test_max_attempt_pending_response_moves_to_dead_letter_queue(self):
        stale_doc = FakeDoc("thread-stale", {
            "threadId": "thread-stale",
            "msgId": "message-1",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nThanks",
            "clientId": "client-1",
            "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
            "lastError": "Graph failed repeatedly",
        })
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([stale_doc, active_doc])

        with patch("email_automation.clients._fs", fake_fs):
            valid = pending_responses.get_pending_responses("uid-1")

        self.assertEqual([item["doc"].id for item in valid], ["thread-active"])
        self.assertTrue(stale_doc.reference.deleted)
        dead_letter = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["originalDocId"], "thread-stale")
        self.assertEqual(dead_letter["threadId"], "thread-stale")
        self.assertEqual(dead_letter["recipient"], "bp21harrison@gmail.com")
        self.assertEqual(dead_letter["failureReason"], "Graph failed repeatedly")


if __name__ == "__main__":
    unittest.main()
