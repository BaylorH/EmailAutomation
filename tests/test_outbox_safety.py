import unittest
import os
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import email as email_module
from email_automation import notifications as notifications_module


class FakeDocRef:
    def __init__(self, doc_id="outbox-1"):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []
        self.update_calls = []

    def delete(self):
        self.deleted = True

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))

    def update(self, data):
        self.update_calls.append(data)


class FakeDoc:
    def __init__(self, data, doc_id="outbox-1"):
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


class OutboxSafetyTests(unittest.TestCase):
    def test_cancel_requested_item_is_deleted_without_sending(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi,\n\nPlease disregard.\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Cancel St, Testville",
            "rowNumber": 12,
            "cancelRequested": True,
            "status": "cancel_requested",
        })

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email, \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)

    def test_exact_or_threaded_dashboard_items_are_not_grouped_with_campaign_outreach(self):
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "threadId": "thread-1",
            "replyToMessageId": "message-1",
            "scriptSelectionMode": "exact",
        }))
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "notificationId": "notification-1",
            "forceScript": True,
        }))
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "source": "dashboard_tour_planner",
        }))
        self.assertTrue(email_module._must_process_outbox_item_individually({
            "actionType": "tour_invite",
        }))
        self.assertFalse(email_module._must_process_outbox_item_individually({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Campaign first touch",
        }))

    def test_tour_planner_outbox_uses_reviewed_body_even_for_existing_contact(self):
        reviewed_body = (
            "Property: 555 Geocoded Map Dr\n"
            "Scheduled arrival: 9:00 AM\n"
            "Scheduled departure: 9:30 AM\n"
            "Please confirm whether this tour slot works."
        )
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": reviewed_body,
            "clientId": "client-1",
            "subject": "Tour slot: 555 Geocoded Map Dr at 9:00 AM",
            "rowNumber": 7,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "actionAuditId": "audit-tour",
        }, doc_id="outbox-tour")

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value="Wrong fallback body") as select_script, \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison@gmail.com"],
                 "errors": {},
             }) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        select_script.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(send_and_index_email.call_args.args[2], reviewed_body)

    def test_successful_dashboard_outbox_finalizes_notification_and_thread_after_send(self):
        outbox_ref = FakeDocRef()
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "delete_notification_and_decrement_counters") as delete_notification:
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                {
                    "clientId": "client-1",
                    "notificationClientId": "client-1",
                    "notificationId": "notification-1",
                    "deleteNotificationOnSend": True,
                    "resumeThreadOnSend": True,
                    "threadId": "thread-1",
                },
            )

        self.assertTrue(outbox_ref.deleted)
        delete_notification.assert_called_once_with("uid-1", "client-1", "notification-1")
        thread_set = fake_fs.set_calls[0]
        self.assertEqual(
            thread_set[0],
            ("collection", "users", "document", "uid-1", "collection", "threads", "document", "thread-1"),
        )
        self.assertEqual(thread_set[1]["status"], "active")
        self.assertEqual(thread_set[1]["followUpStatus"], "waiting")
        self.assertTrue(thread_set[2])

    def test_decrement_notification_rollups_clamps_counts(self):
        updated = notifications_module._decrement_notification_rollups(
            {
                "notificationsUnread": 1,
                "newUpdateCount": 0,
                "notifCounts": {"action_needed": 1, "sheet_update": 3},
            },
            "action_needed",
        )

        self.assertEqual(updated["notificationsUnread"], 0)
        self.assertEqual(updated["newUpdateCount"], 0)
        self.assertEqual(updated["notifCounts"], {"sheet_update": 3})

    def test_decrement_notification_rollups_handles_sheet_update_count(self):
        updated = notifications_module._decrement_notification_rollups(
            {
                "notificationsUnread": 4,
                "newUpdateCount": 2,
                "notifCounts": {"sheet_update": 2},
            },
            "sheet_update",
        )

        self.assertEqual(updated["notificationsUnread"], 3)
        self.assertEqual(updated["newUpdateCount"], 1)
        self.assertEqual(updated["notifCounts"], {"sheet_update": 1})

    def test_duplicate_suppression_terminalizes_action_audit(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Duplicate St, Testville",
            "rowNumber": 12,
            "actionAuditId": "audit-duplicate",
        }, doc_id="outbox-duplicate")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=True), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "duplicate_skipped")
        self.assertEqual(audit_payload["outboxId"], "outbox-duplicate")

    def test_contact_opt_out_terminalizes_action_audit_for_grouped_item(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Opt Out St, Testville",
            "rowNumber": 12,
            "actionAuditId": "audit-opt-out",
        }, doc_id="outbox-opt-out")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch("email_automation.processing.is_contact_opted_out", return_value={"reason": "unsubscribe"}), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                "bp21harrison@gmail.com",
                [{"doc": doc, "data": doc.to_dict()}],
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "opt_out_skipped")
        self.assertEqual(audit_payload["outboxId"], "outbox-opt-out")


if __name__ == "__main__":
    unittest.main()
