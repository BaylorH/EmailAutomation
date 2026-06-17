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


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return self._data


class FakeOutboxCollection:
    def __init__(self, docs):
        self.docs = docs

    def order_by(self, _field):
        return self

    def stream(self):
        return self.docs


class FakeUserNode:
    def __init__(self, docs, user_data=None):
        self.docs = docs
        self.user_data = user_data or {"email": "baylor.freelance@outlook.com"}

    def get(self):
        return FakeSnapshot(self.user_data)

    def collection(self, name):
        if name != "outbox":
            raise AssertionError(f"Unexpected user collection: {name}")
        return FakeOutboxCollection(self.docs)


class FakeUsersCollection:
    def __init__(self, docs, user_data=None):
        self.docs = docs
        self.user_data = user_data

    def document(self, _user_id):
        return FakeUserNode(self.docs, self.user_data)


class FakeFirestoreWithOutbox:
    def __init__(self, docs, user_data=None):
        self.docs = docs
        self.user_data = user_data

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"Unexpected root collection: {name}")
        return FakeUsersCollection(self.docs, self.user_data)


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

    def test_send_outboxes_requests_fresh_headers_for_each_throttled_recipient(self):
        docs = [
            FakeDoc({
                "assignedEmails": ["bp21harrison+one@gmail.com"],
                "script": "Hi Avery",
                "clientId": "client-1",
                "subject": "100 Token Way",
                "rowNumber": 3,
            }, doc_id="outbox-1"),
            FakeDoc({
                "assignedEmails": ["bp21harrison+two@gmail.com"],
                "script": "Hi Blake",
                "clientId": "client-1",
                "subject": "200 Token Way",
                "rowNumber": 4,
            }, doc_id="outbox-2"),
        ]
        provider_calls = []
        send_headers = []

        def headers_provider():
            provider_calls.append(len(provider_calls) + 1)
            return {
                "Authorization": f"Bearer fresh-token-{provider_calls[-1]}",
                "Content-Type": "application/json",
            }

        def record_single_send(_user_id, headers, _item, *_args, **_kwargs):
            send_headers.append(headers["Authorization"])

        with patch("email_automation.clients._fs", FakeFirestoreWithOutbox(docs)), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send), \
             patch.object(email_module.time, "sleep", return_value=None) as sleep:
            email_module.send_outboxes(
                "uid-1",
                {"Authorization": "Bearer stale-token"},
                headers_provider=headers_provider,
            )

        self.assertEqual(provider_calls, [1, 2])
        self.assertEqual(send_headers, ["Bearer fresh-token-1", "Bearer fresh-token-2"])
        sleep.assert_called_once_with(120)

    def test_send_outboxes_resolves_structured_professional_signature_before_send(self):
        docs = [
            FakeDoc({
                "assignedEmails": ["bp21harrison@gmail.com"],
                "script": "Hi Avery",
                "clientId": "client-1",
                "subject": "100 Signature Way",
                "rowNumber": 3,
            }, doc_id="outbox-1")
        ]
        captured_signature = {}

        def record_single_send(_user_id, _headers, _item, user_signature=None, signature_mode=None, user_email=None, **_kwargs):
            captured_signature["html"] = user_signature
            captured_signature["mode"] = signature_mode
            captured_signature["email"] = user_email

        with patch(
            "email_automation.clients._fs",
            FakeFirestoreWithOutbox(docs, user_data={
                "email": "baylor.freelance@outlook.com",
                "signatureMode": "professional",
                "emailSignature": '<div data-sitesift-professional-signature="v1">Jill Ames jill.ames@mohrpartners.com</div>',
                "professionalSignature": {
                    "name": "John Doe",
                    "title": "Principal",
                    "email": "baylor.freelance@outlook.com",
                    "company": "Example Realty Advisors",
                },
            }),
        ), patch.object(email_module, "_send_single_outbox_item", side_effect=record_single_send):
            email_module.send_outboxes(
                "uid-1",
                {"Authorization": "Bearer token"},
            )

        self.assertEqual(captured_signature["mode"], "professional")
        self.assertEqual(captured_signature["email"], "baylor.freelance@outlook.com")
        self.assertIn("John Doe", captured_signature["html"])
        self.assertIn("Example Realty Advisors", captured_signature["html"])
        self.assertNotIn("Jill Ames", captured_signature["html"])
        self.assertNotIn("jill.ames@mohrpartners.com", captured_signature["html"])

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
