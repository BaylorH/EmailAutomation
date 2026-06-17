import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
for candidate_credentials in [
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
]:
    if os.path.exists(candidate_credentials):
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", candidate_credentials)
        break

from email_automation.sheet_operations import complete_threads_for_row, stop_threads_for_row


class FakeThreadDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = dict(data)
        self.updates = []

    def to_dict(self):
        return dict(self._data)

    def update(self, payload):
        self.updates.append(payload)
        self._data.update(payload)


class FakeThreadsCollection:
    def __init__(self, docs):
        self.docs = {doc.id: doc for doc in docs}

    def stream(self):
        return list(self.docs.values())

    def document(self, doc_id):
        return self.docs[doc_id]


class FakeFirestore:
    def __init__(self, threads_collection):
        self.threads_collection = threads_collection

    def collection(self, name):
        return self

    def document(self, doc_id):
        return self

    def stream(self):
        return self.threads_collection.stream()

    def update(self, payload):
        raise AssertionError("update should be called on a thread document")


class FakeUserDocument:
    def __init__(self, threads_collection):
        self.threads_collection = threads_collection

    def collection(self, name):
        if name != "threads":
            raise AssertionError(f"unexpected collection {name}")
        return self.threads_collection


class FakeUsersCollection:
    def __init__(self, threads_collection):
        self.threads_collection = threads_collection

    def document(self, user_id):
        return FakeUserDocument(self.threads_collection)


class FakeRootFirestore:
    def __init__(self, threads_collection):
        self.threads_collection = threads_collection

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"unexpected root collection {name}")
        return FakeUsersCollection(self.threads_collection)


class SplitThreadTerminalStateTests(unittest.TestCase):
    def test_stop_threads_for_row_terminalizes_all_split_roots_for_moved_property(self):
        original_root = FakeThreadDoc("original-root", {
            "clientId": "client-1",
            "rowNumber": 10,
            "status": "active",
            "followUpStatus": "waiting",
        })
        reply_root = FakeThreadDoc("reply-root", {
            "clientId": "client-1",
            "rowNumber": 10,
            "status": "stopped",
            "followUpStatus": "stopped",
        })
        shifted_row = FakeThreadDoc("shifted-row", {
            "clientId": "client-1",
            "rowNumber": 9,
            "status": "active",
            "followUpStatus": "waiting",
        })
        other_client = FakeThreadDoc("other-client", {
            "clientId": "client-2",
            "rowNumber": 10,
            "status": "active",
            "followUpStatus": "waiting",
        })
        threads = FakeThreadsCollection([original_root, reply_root, shifted_row, other_client])

        with patch("email_automation.sheet_operations._fs", FakeRootFirestore(threads)):
            updated = stop_threads_for_row(
                "uid-1",
                row_number=10,
                client_id="client-1",
                reason="property_unavailable",
            )

        self.assertEqual(2, updated)
        for doc in (original_root, reply_root):
            self.assertEqual("stopped", doc._data["status"])
            self.assertEqual("stopped", doc._data["followUpStatus"])
            self.assertEqual("property_unavailable", doc._data["statusReason"])
            self.assertIsNone(doc._data["followUpConfig.processingBy"])
            self.assertIsNone(doc._data["followUpConfig.processingAt"])

        self.assertFalse(shifted_row.updates)
        self.assertFalse(other_client.updates)

    def test_complete_threads_for_row_closes_active_split_roots_after_tour_confirmation(self):
        original_root = FakeThreadDoc("original-root", {
            "clientId": "client-1",
            "rowNumber": 6,
            "status": "active",
            "followUpStatus": "waiting",
        })
        tour_root = FakeThreadDoc("tour-root", {
            "clientId": "client-1",
            "rowNumber": 6,
            "status": "completed",
            "followUpStatus": "waiting",
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
        })
        paused_action = FakeThreadDoc("paused-action", {
            "clientId": "client-1",
            "rowNumber": 6,
            "status": "paused",
            "statusReason": "needs_user_input",
            "followUpStatus": "paused",
        })
        stopped_nonviable = FakeThreadDoc("stopped-nonviable", {
            "clientId": "client-1",
            "rowNumber": 6,
            "status": "stopped",
            "statusReason": "property_unavailable",
            "followUpStatus": "stopped",
        })
        other_client = FakeThreadDoc("other-client", {
            "clientId": "client-2",
            "rowNumber": 6,
            "status": "active",
            "followUpStatus": "waiting",
        })
        threads = FakeThreadsCollection([
            original_root,
            tour_root,
            paused_action,
            stopped_nonviable,
            other_client,
        ])

        with patch("email_automation.sheet_operations._fs", FakeRootFirestore(threads)):
            updated = complete_threads_for_row(
                "uid-1",
                row_number=6,
                client_id="client-1",
                reason="tour_confirmed",
            )

        self.assertEqual(2, updated)
        for doc in (original_root, tour_root):
            self.assertEqual("completed", doc._data["status"])
            self.assertEqual("stopped", doc._data["followUpStatus"])
            self.assertEqual("tour_confirmed", doc._data["statusReason"])
            self.assertIsNone(doc._data["followUpConfig.processingBy"])
            self.assertIsNone(doc._data["followUpConfig.processingAt"])

        self.assertFalse(paused_action.updates)
        self.assertFalse(stopped_nonviable.updates)
        self.assertFalse(other_client.updates)


if __name__ == "__main__":
    unittest.main()
