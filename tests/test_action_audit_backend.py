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


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"Unexpected HTTP status {self.status_code}")


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

    def test_successful_tour_invite_send_marks_thread_awaiting_confirmation(self):
        outbox_ref = FakeOutboxRef("outbox-tour")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs):
            email_module._finalize_successful_outbox_item(
                "uid-1",
                outbox_ref,
                {
                    "assignedEmails": ["bp21harrison@gmail.com"],
                    "clientId": "client-1",
                    "threadId": "thread-tour",
                    "actionType": "tour_invite",
                    "actionAuditId": "audit-tour",
                    "tourInvite": {
                        "arrivalTime": "10:47 AM",
                        "departureTime": "11:17 AM",
                    },
                },
                send_result={
                    "sentMessageIds": {"bp21harrison@gmail.com": "graph-tour-message"},
                    "internetMessageIds": {"bp21harrison@gmail.com": "<tour-message@example.com>"},
                    "conversationIds": {"bp21harrison@gmail.com": "conversation-tour"},
                },
            )

        thread_updates = [
            call for call in fake_fs.set_calls
            if call[0] == (
                "collection", "users", "document", "uid-1",
                "collection", "threads", "document", "thread-tour",
            )
        ]
        self.assertEqual(1, len(thread_updates))
        payload = thread_updates[0][1]
        self.assertEqual("awaiting_confirmation", payload["tourStatus"])
        self.assertEqual("sent", payload["tourInvite.status"])
        self.assertEqual(email_module.SERVER_TIMESTAMP, payload["tourInvite.sentAt"])
        self.assertEqual("graph-tour-message", payload["tourInvite.sentMessageId"])
        self.assertEqual("<tour-message@example.com>", payload["tourInvite.internetMessageId"])
        self.assertEqual("conversation-tour", payload["tourInvite.conversationId"])

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

    def test_dead_letter_copy_overrides_retry_metadata(self):
        outbox_ref = FakeOutboxRef("outbox-dead")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs):
            email_module._move_to_dead_letter(
                "uid-1",
                outbox_ref,
                {
                    "assignedEmails": ["bp21harrison+leaguecity-row20@gmail.com"],
                    "clientId": "client-1",
                    "rowNumber": 20,
                    "actionAuditId": "audit-dead",
                    "status": "retrying",
                    "attempts": 4,
                    "maxAttempts": email_module.MAX_OUTBOX_ATTEMPTS,
                    "lastError": "Request failed after 3 attempts",
                },
                "Send errors after 5 attempts: Request failed after 3 attempts",
            )

        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertEqual(dead_letter_payload["attempts"], email_module.MAX_OUTBOX_ATTEMPTS)
        self.assertEqual(dead_letter_payload["maxAttempts"], email_module.MAX_OUTBOX_ATTEMPTS)
        self.assertEqual(dead_letter_payload["lastError"], "Send errors after 5 attempts: Request failed after 3 attempts")
        self.assertEqual(dead_letter_payload["failureReason"], "Send errors after 5 attempts: Request failed after 3 attempts")
        self.assertEqual(dead_letter_payload["originalDocId"], "outbox-dead")

    def test_tour_invite_outbox_context_preserves_property_anchor(self):
        context = email_module._thread_context_from_outbox({
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": {"arrivalTime": "9:00 AM"},
            "property": {"address": "0 Gemini Ave", "city": "Houston"},
        })

        self.assertEqual("dashboard_tour_planner", context["source"])
        self.assertEqual("tour_invite", context["actionType"])
        self.assertEqual({"arrivalTime": "9:00 AM"}, context["tourInvite"])
        self.assertEqual({"address": "0 Gemini Ave", "city": "Houston"}, context["property"])

    @patch.object(email_module.time, "sleep", return_value=None)
    @patch.object(email_module.requests, "post")
    @patch.object(email_module.requests, "get")
    def test_graph_reply_send_returns_sent_item_identity(self, requests_get, requests_post, _sleep):
        requests_get.side_effect = [
            FakeResponse(200, {
                "conversationId": "conversation-1",
                "subject": "RE: 910 Confidential Ct",
            }),
            FakeResponse(200, {
                "value": [{
                    "id": "graph-message-1",
                    "internetMessageId": "<internet-message-1@example.com>",
                    "conversationId": "conversation-1",
                    "subject": "RE: 910 Confidential Ct",
                    "sentDateTime": "2026-06-06T23:57:12Z",
                }]
            }),
        ]
        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [],
                })
            if url.endswith("/reply-draft-1/send"):
                return FakeResponse(202)
            if url.endswith("/attachments"):
                return FakeResponse(201)
            return FakeResponse(500)

        requests_post.side_effect = fake_post

        with patch.object(email_module.requests, "patch", return_value=FakeResponse(200)), \
                patch("email_automation.processing.is_contact_opted_out", return_value=None):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Morgan,\n\nThanks.",
                "reply-message-1",
                "thread-1",
                user_signature="Baylor Harrison\nbaylor.freelance@outlook.com",
                signature_mode="professional",
                user_email="baylor.freelance@outlook.com",
            )

        self.assertTrue(result["sent"])
        self.assertEqual(result["sentMessageId"], "graph-message-1")
        self.assertEqual(result["internetMessageId"], "<internet-message-1@example.com>")
        self.assertEqual(result["conversationId"], "conversation-1")

    @patch.object(email_module.time, "sleep", return_value=None)
    @patch("email_automation.processing.is_contact_opted_out", return_value=None)
    @patch.object(email_module, "lookup_thread_by_message_id")
    @patch.object(email_module, "index_conversation_id", return_value=True)
    @patch.object(email_module, "index_message_id", return_value=True)
    @patch.object(email_module, "save_message", return_value=True)
    @patch.object(email_module, "save_thread_root", return_value=True)
    @patch.object(email_module.requests, "post")
    @patch.object(email_module.requests, "get")
    def test_tour_invite_thread_indexes_actual_property_address(
        self,
        requests_get,
        requests_post,
        save_thread_root,
        _save_message,
        _index_message_id,
        _index_conversation_id,
        _lookup_thread_by_message_id,
        _is_contact_opted_out,
        _sleep,
    ):
        normalized_message_id = email_module.normalize_message_id("<normalized-tour-message@example.com>")
        _lookup_thread_by_message_id.return_value = normalized_message_id
        requests_post.side_effect = [
            FakeResponse(201, {"id": "draft-tour-1"}),
            FakeResponse(202),
        ]
        requests_get.return_value = FakeResponse(200, {
            "internetMessageId": "<normalized-tour-message@example.com>",
            "conversationId": "conversation-tour-1",
            "subject": "Tour slot: 0 Gemini Ave at 9:00 AM",
            "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
        })

        result = email_module.send_and_index_email(
            "uid-1",
            {"Authorization": "Bearer token"},
            "Hi Ron,\n\nPlease confirm the 9:00 AM tour slot for 0 Gemini Ave.",
            ["bp21harrison@gmail.com"],
            client_id_or_none="client-1",
            row_number=20,
            subject_override="Tour slot: 0 Gemini Ave at 9:00 AM",
            thread_context={
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": {"arrivalTime": "9:00 AM", "departureTime": "9:30 AM"},
                "property": {"address": "0 Gemini Ave", "city": "Houston", "state": "TX"},
            },
        )

        self.assertEqual(["bp21harrison@gmail.com"], result["sent"])
        thread_meta = save_thread_root.call_args[0][2]
        self.assertEqual("Tour slot: 0 Gemini Ave at 9:00 AM", thread_meta["subject"])
        self.assertEqual("0 Gemini Ave, Houston", thread_meta["propertyAddress"])
        self.assertEqual("dashboard_tour_planner", thread_meta["source"])
        self.assertEqual("tour_invite", thread_meta["actionType"])
        self.assertEqual({"arrivalTime": "9:00 AM", "departureTime": "9:30 AM"}, thread_meta["tourInvite"])


if __name__ == "__main__":
    unittest.main()
