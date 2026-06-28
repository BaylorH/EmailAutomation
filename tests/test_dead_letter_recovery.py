import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import dead_letter_recovery
from email_automation.sent_mail_guard import SentMailGuardLookupError


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class FakeFirestoreNode:
    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []
        self.id = self.path[-1] if self.path else "root"

    def collection(self, name):
        return FakeFirestoreNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeFirestoreNode(self.root, self.path + ["document", name])

    def get(self):
        return FakeSnapshot(self.root.docs.get(tuple(self.path)), tuple(self.path) in self.root.docs)

    def set(self, data, merge=False):
        self.root.set_calls.append((tuple(self.path), data, merge))
        existing = self.root.docs.get(tuple(self.path), {}) if merge else {}
        self.root.docs[tuple(self.path)] = {**existing, **data}

    def update(self, data):
        self.root.update_calls.append((tuple(self.path), data))
        existing = self.root.docs.get(tuple(self.path), {})
        self.root.docs[tuple(self.path)] = {**existing, **data}

    def add(self, data):
        self.root.add_calls.append((tuple(self.path), data))
        doc_id = f"auto-{len(self.root.add_calls)}"
        doc_path = tuple(self.path + ["document", doc_id])
        self.root.docs[doc_path] = dict(data)
        return FakeFirestoreNode(self.root, list(doc_path))


class FakeFirestore:
    def __init__(self, dead_letter_payload):
        self.docs = {}
        self.add_calls = []
        self.set_calls = []
        self.update_calls = []
        self.dead_letter_path = (
            "collection", "users", "document", "uid-1",
            "collection", "deadLetterQueue", "document", "dead-1",
        )
        self.docs[self.dead_letter_path] = dict(dead_letter_payload)

    def collection(self, name):
        return FakeFirestoreNode(self, ["collection", name])


def base_dead_letter(**overrides):
    payload = {
        "source": "outbox",
        "status": "dead_lettered",
        "originalDocId": "outbox-old",
        "assignedEmails": ["bp21harrison@gmail.com"],
        "script": "Hi BP21,\n\nCan you confirm availability?\n\nThanks,\nBaylor",
        "subject": "Test property availability",
        "clientId": "client-1",
        "threadId": "thread-1",
        "conversationId": "conversation-1",
        "actionAuditId": "audit-1",
        "attempts": 5,
        "failureReason": "Graph failed repeatedly",
        "lastError": "Graph failed repeatedly",
    }
    payload.update(overrides)
    return payload


class DeadLetterRecoveryTests(unittest.TestCase):
    def fake_clients(self, fake_fs):
        return patch.dict(sys.modules, {"email_automation.clients": SimpleNamespace(_fs=fake_fs)})

    def test_requeue_refuses_already_sent_item_without_writing_outbox(self):
        fake_fs = FakeFirestore(base_dead_letter(alreadySent=True, status="needs_reconciliation"))

        with self.fake_clients(fake_fs):
            result = dead_letter_recovery.resolve_dead_letter_item(
                "uid-1",
                "dead-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer fake"},
                operator_id="operator-1",
            )

        self.assertFalse(result["success"])
        self.assertEqual("unsafe_already_sent", result["code"])
        self.assertEqual([], fake_fs.add_calls)
        self.assertEqual("blocked_already_sent", fake_fs.update_calls[-1][1]["recoveryStatus"])

    def test_requeue_refuses_manual_continuation_without_sent_items_lookup(self):
        fake_fs = FakeFirestore(base_dead_letter(
            failureReason="Queued send stopped because Sent Items shows the user manually continued this conversation",
        ))

        with self.fake_clients(fake_fs), \
             patch.object(dead_letter_recovery, "find_matching_sent_message_for_retry") as sent_guard:
            result = dead_letter_recovery.resolve_dead_letter_item(
                "uid-1",
                "dead-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer fake"},
                operator_id="operator-1",
            )

        self.assertFalse(result["success"])
        self.assertEqual("blocked_manual_continuation", result["code"])
        sent_guard.assert_not_called()
        self.assertEqual([], fake_fs.add_calls)
        self.assertEqual("blocked_manual_continuation", fake_fs.update_calls[-1][1]["recoveryStatus"])

    def test_requeue_matching_sent_item_records_reconciliation_without_resending(self):
        fake_fs = FakeFirestore(base_dead_letter())
        sent_match = {
            "sentMessageId": "sent-graph-1",
            "internetMessageId": "<sent-graph-1@example.com>",
            "conversationId": "conversation-1",
            "sentDateTime": "2026-06-18T10:00:00Z",
        }

        with self.fake_clients(fake_fs), \
             patch.object(dead_letter_recovery, "find_matching_sent_message_for_retry", return_value=sent_match), \
             patch.object(dead_letter_recovery, "find_sent_conversation_continuation_for_retry", return_value=None):
            result = dead_letter_recovery.resolve_dead_letter_item(
                "uid-1",
                "dead-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer fake"},
                operator_id="operator-1",
            )

        self.assertFalse(result["success"])
        self.assertEqual("already_sent", result["code"])
        self.assertEqual([], fake_fs.add_calls)
        update_payload = fake_fs.update_calls[-1][1]
        self.assertEqual("needs_reconciliation", update_payload["status"])
        self.assertTrue(update_payload["alreadySent"])
        self.assertEqual("sent-graph-1", update_payload["sentMessageId"])

    def test_requeue_guard_lookup_failure_keeps_item_visible_without_resending(self):
        fake_fs = FakeFirestore(base_dead_letter())

        with self.fake_clients(fake_fs), \
             patch.object(dead_letter_recovery, "find_matching_sent_message_for_retry", side_effect=SentMailGuardLookupError("Graph 401")):
            result = dead_letter_recovery.resolve_dead_letter_item(
                "uid-1",
                "dead-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer fake"},
                operator_id="operator-1",
            )

        self.assertFalse(result["success"])
        self.assertEqual("guard_unreadable", result["code"])
        self.assertEqual([], fake_fs.add_calls)
        self.assertEqual("blocked_guard_unreadable", fake_fs.update_calls[-1][1]["recoveryStatus"])

    def test_requeue_verified_unsent_outbox_item_creates_fresh_outbox_and_updates_audit(self):
        fake_fs = FakeFirestore(base_dead_letter())

        with self.fake_clients(fake_fs), \
             patch.object(dead_letter_recovery, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(dead_letter_recovery, "find_sent_conversation_continuation_for_retry", return_value=None):
            result = dead_letter_recovery.resolve_dead_letter_item(
                "uid-1",
                "dead-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer fake"},
                operator_id="operator-1",
                note="Verified unsent in Sent Items",
            )

        self.assertTrue(result["success"])
        self.assertEqual("requeued", result["code"])
        outbox_path, outbox_payload = fake_fs.add_calls[-1]
        self.assertEqual(("collection", "users", "document", "uid-1", "collection", "outbox"), outbox_path)
        self.assertEqual(0, outbox_payload["attempts"])
        self.assertEqual("queued", outbox_payload["status"])
        self.assertEqual("dead-1", outbox_payload["recoveryFromDeadLetterId"])
        self.assertNotIn("failureReason", outbox_payload)
        self.assertNotIn("alreadySent", outbox_payload)
        self.assertEqual("requeued", fake_fs.update_calls[-1][1]["status"])
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("queued", audit_payload["status"])
        self.assertEqual("auto-1", audit_payload["outboxId"])


if __name__ == "__main__":
    unittest.main()
