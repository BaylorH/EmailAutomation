import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import email as email_module


class FakeOutboxRef:
    def __init__(self):
        self.deleted = False

    def delete(self):
        self.deleted = True


class FakeFirestoreNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        return FakeFirestoreNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeFirestoreNode(self.root, self.path + ["document", name])

    def delete(self):
        self.root.deleted_paths.append(tuple(self.path))

    def set(self, data, merge=False):
        self.root.set_calls.append((tuple(self.path), data, merge))


class FakeFirestore:
    def __init__(self):
        self.deleted_paths = []
        self.set_calls = []

    def collection(self, name):
        return FakeFirestoreNode(self, ["collection", name])


class BackendActionAuditTests(unittest.TestCase):
    def test_successful_dashboard_send_marks_action_audit_sent(self):
        outbox_ref = FakeOutboxRef()
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs):
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                {
                    "clientId": "client-1",
                    "notificationId": "notification-1",
                    "threadId": "thread-1",
                    "actionAuditId": "audit-1",
                },
            )

        self.assertTrue(outbox_ref.deleted)
        self.assertIn(
            (
                ("collection", "users", "document", "uid-1", "collection", "actionAudit", "document", "audit-1"),
                {
                    "status": "sent",
                    "outboxId": None,
                    "clientId": "client-1",
                    "notificationId": "notification-1",
                    "threadId": "thread-1",
                    "sentAt": email_module.SERVER_TIMESTAMP,
                    "updatedAt": email_module.SERVER_TIMESTAMP,
                },
                True,
            ),
            fake_fs.set_calls,
        )


if __name__ == "__main__":
    unittest.main()
