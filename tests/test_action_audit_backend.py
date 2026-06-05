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
    def __init__(self, doc_id="outbox-1"):
        self.id = doc_id
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

    def add(self, data):
        self.root.add_calls.append((tuple(self.path), data))
        return FakeFirestoreNode(self.root, self.path + ["document", "auto-id"])


class FakeFirestore:
    def __init__(self):
        self.deleted_paths = []
        self.set_calls = []
        self.add_calls = []

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
                    "outboxId": "outbox-1",
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

    def test_successful_send_persists_graph_message_identity_in_action_audit(self):
        outbox_ref = FakeOutboxRef("outbox-graph-id")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs):
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                {
                    "assignedEmails": ["bp21harrison@gmail.com"],
                    "clientId": "client-1",
                    "notificationId": "notification-1",
                    "threadId": "thread-1",
                    "actionAuditId": "audit-1",
                },
                send_result={
                    "sentMessageIds": {"bp21harrison@gmail.com": "graph-message-1"},
                    "internetMessageIds": {"bp21harrison@gmail.com": "<internet-message-1@example.com>"},
                    "threadIds": {"bp21harrison@gmail.com": "thread-graph-1"},
                    "conversationIds": {"bp21harrison@gmail.com": "conversation-graph-1"},
                },
            )

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "sent")
        self.assertEqual(audit_payload["outboxId"], "outbox-graph-id")
        self.assertEqual(audit_payload["sentMessageId"], "graph-message-1")
        self.assertEqual(audit_payload["internetMessageId"], "<internet-message-1@example.com>")
        self.assertEqual(audit_payload["sentThreadId"], "thread-graph-1")
        self.assertEqual(audit_payload["conversationId"], "conversation-graph-1")

    def test_cancelled_outbox_terminalizes_action_audit(self):
        outbox_ref = FakeOutboxRef("outbox-cancel")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs):
            cancelled = email_module._delete_cancelled_outbox_item_if_needed(
                outbox_ref,
                {
                    "clientId": "client-1",
                    "notificationId": "notification-1",
                    "threadId": "thread-1",
                    "actionAuditId": "audit-cancel",
                    "cancelRequested": True,
                    "status": "cancel_requested",
                },
                user_id="uid-1",
            )

        self.assertTrue(cancelled)
        self.assertTrue(outbox_ref.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "cancelled")
        self.assertEqual(audit_payload["outboxId"], "outbox-cancel")
        self.assertEqual(audit_payload["clientId"], "client-1")
        self.assertEqual(audit_payload["notificationId"], "notification-1")
        self.assertEqual(audit_payload["threadId"], "thread-1")

    def test_dead_letter_terminalizes_action_audit_as_dead_lettered(self):
        outbox_ref = FakeOutboxRef("outbox-dead")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs):
            email_module._move_to_dead_letter(
                "uid-1",
                outbox_ref,
                {
                    "clientId": "client-1",
                    "notificationId": "notification-1",
                    "threadId": "thread-1",
                    "actionAuditId": "audit-dead",
                    "status": "claimed",
                },
                "Graph returned 500",
            )

        self.assertTrue(outbox_ref.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "dead_lettered")
        self.assertEqual(audit_payload["failureReason"], "Graph returned 500")


if __name__ == "__main__":
    unittest.main()
