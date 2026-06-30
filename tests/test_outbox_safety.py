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

    def get(self):
        return FakeSnapshot({}, exists=False)


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

    def test_paused_client_outbox_item_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("paused-outbox")
        data = {"clientId": "client-1", "script": "Hi Avery"}

        with patch.object(
            email_module,
            "get_client_automation_pause",
            return_value=(True, "admin_incident_pause", {"automationPaused": True}),
        ), patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._pause_client_outbox_item_if_needed(
                "uid-1",
                doc_ref,
                data,
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("paused/stopped", move_to_dead_letter.call_args.args[3])

    def test_jill_tour_outbox_item_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("jill-tour-outbox")
        data = {
            "clientId": "client-1",
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "script": "Hi Avery,\n\nPlease confirm the 10:00 AM tour slot.",
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._pause_results_outbox_item_if_needed(
                "C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
                doc_ref,
                data,
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("Tour-planning emails", move_to_dead_letter.call_args.args[3])

    def test_unresolved_name_placeholder_outbox_item_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("unsafe-outbox")
        data = {"clientId": "client-1", "script": "Hi [NAME],\n\nCould you confirm?"}

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_unsafe_outbound_body_if_needed(
                "uid-1",
                doc_ref,
                data,
                data["script"],
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("[NAME]", move_to_dead_letter.call_args.args[3])

    def test_normal_campaign_tour_language_moves_to_dead_letter_before_send(self):
        doc_ref = FakeDocRef("tour-language-outbox")
        data = {
            "clientId": "client-1",
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
            "script": (
                "Hi Connor,\n\nBefore we proceed with tour scheduling and/or LOIs, "
                "can you please confirm the following?"
            ),
        }

        with patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            blocked = email_module._dead_letter_unsafe_outbound_body_if_needed(
                "uid-1",
                doc_ref,
                data,
                data["script"],
            )

        self.assertTrue(blocked)
        move_to_dead_letter.assert_called_once()
        self.assertIn("Tour/LOI", move_to_dead_letter.call_args.args[3])

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
             patch("email_automation.clients._fs", FakeFirestore()), \
             patch.object(email_module, "_select_script_for_recipient", return_value="Wrong fallback body") as select_script, \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison@gmail.com"],
                 "errors": {},
             }) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        select_script.assert_not_called()
        send_and_index_email.assert_called_once()
        self.assertEqual(send_and_index_email.call_args.args[2], reviewed_body)

    def test_tour_planner_outbox_preserves_tour_context_on_sent_thread(self):
        tour_context = {
            "propertyId": "row-7",
            "arrivalTime": "10:47 AM",
            "departureTime": "11:17 AM",
            "stopMinutes": 30,
        }
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison@gmail.com"],
            "script": "Hi Avery,\n\nPlease confirm the 10:47 AM tour slot.",
            "clientId": "client-1",
            "subject": "Tour slot: 555 Geocoded Map Dr at 10:47 AM",
            "rowNumber": 7,
            "source": "dashboard_tour_planner",
            "actionType": "tour_invite",
            "tourInvite": tour_context,
            "actionAuditId": "audit-tour",
        }, doc_id="outbox-tour")

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch("email_automation.clients._fs", FakeFirestore()), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison@gmail.com"],
                 "errors": {},
             }) as send_and_index_email, \
             patch.object(email_module, "_finalize_successful_outbox_item"):
            email_module._send_single_outbox_item(
                "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertEqual(
            {
                "source": "dashboard_tour_planner",
                "actionType": "tour_invite",
                "tourInvite": tour_context,
                "actionAuditId": "audit-tour",
            },
            send_and_index_email.call_args.kwargs["thread_context"],
        )

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

    def test_retryable_send_failure_updates_action_audit_with_visible_retry_state(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+leaguecity-row20@gmail.com"],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 1,
            "actionAuditId": "audit-retry",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-retry")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [],
                 "errors": {
                     "bp21harrison+leaguecity-row20@gmail.com": "Request failed after 3 attempts",
                 },
             }):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertFalse(doc.reference.deleted)
        retry_payload = doc.reference.set_calls[-1][0][0]
        self.assertEqual(retry_payload["attempts"], 2)
        self.assertEqual(retry_payload["processingBy"], None)
        self.assertEqual(retry_payload["processingAt"], None)
        self.assertIn("Request failed after 3 attempts", retry_payload["lastError"])

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "retrying")
        self.assertEqual(audit_payload["outboxId"], "outbox-retry")
        self.assertEqual(audit_payload["attempts"], 2)
        self.assertEqual(audit_payload["maxAttempts"], email_module.MAX_OUTBOX_ATTEMPTS)
        self.assertIn("Request failed after 3 attempts", audit_payload["lastError"])

    def test_retry_with_matching_sent_item_moves_to_reconciliation_without_resending(self):
        recipient = "bp21harrison+leaguecity-row20@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 1,
            "lastError": "HTTPSConnectionPool read timed out",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-ambiguous-retry",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-ambiguous-retry")
        fake_fs = FakeFirestore()
        sent_match = {
            "id": "sent-graph-1",
            "internetMessageId": "<sent-graph-1@example.com>",
            "conversationId": "conversation-1",
            "subject": "0 Gemini Ave, Houston",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=sent_match, create=True) as sent_guard, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        sent_guard.assert_called_once()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["sentRecipients"], [recipient])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "sent-graph-1")
        self.assertEqual(dead_letter_payload["internetMessageIds"][recipient], "<sent-graph-1@example.com>")

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "needs_reconciliation")
        self.assertTrue(audit_payload["alreadySent"])
        self.assertEqual(audit_payload["sentRecipients"], [recipient])

    def test_recovered_dead_letter_outbox_checks_sent_items_before_send(self):
        recipient = "bp21harrison+recovered@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 0,
            "status": "queued",
            "requiresSentItemsPreflight": True,
            "recoveryFromDeadLetterId": "dead-1",
            "recoveredAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-recovered",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-recovered")
        fake_fs = FakeFirestore()
        sent_match = {
            "id": "sent-recovered-1",
            "internetMessageId": "<sent-recovered-1@example.com>",
            "conversationId": "conversation-1",
            "subject": "0 Gemini Ave, Houston",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=sent_match, create=True) as sent_guard, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        sent_guard.assert_called_once()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "sent-recovered-1")

    def test_recovered_dead_letter_outbox_blocks_manual_continuation_before_send(self):
        recipient = "bp21harrison+recovered@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "attempts": 0,
            "status": "queued",
            "requiresSentItemsPreflight": True,
            "recoveryFromDeadLetterId": "dead-1",
            "recoveredAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-recovered",
        }, doc_id="outbox-recovered")
        fake_fs = FakeFirestore()
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-thread-1",
            "sentDateTime": "2026-06-26T12:04:00Z",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_thread_row_number", return_value=7), \
             patch.object(email_module, "_get_reply_message_sender", return_value=recipient), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conv-thread-1",
                 "subject": "RE: 0 Gemini Ave",
             }), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None, create=True), \
             patch.object(email_module, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation, create=True) as continuation_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        continuation_guard.assert_called_once()
        send_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("manually continued", dead_letter_payload["failureReason"])

    def test_thread_reply_retry_passes_conversation_identity_to_sent_guard(self):
        recipient = "bp21harrison+reply@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nThat time works.\n\nThanks",
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-thread-retry",
        }, doc_id="outbox-thread-retry")
        fake_fs = FakeFirestore()
        sent_match = {
            "id": "sent-reply-1",
            "internetMessageId": "<sent-reply-1@example.com>",
            "conversationId": "conv-thread-1",
            "subject": "RE: 0 Gemini Ave",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_thread_row_number", return_value=7), \
             patch.object(email_module, "_get_reply_message_sender", return_value=recipient), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conv-thread-1",
                 "subject": "RE: 0 Gemini Ave",
             }), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=sent_match, create=True) as sent_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        sent_guard.assert_called_once()
        self.assertEqual(sent_guard.call_args.kwargs["conversation_id"], "conv-thread-1")
        self.assertEqual(sent_guard.call_args.kwargs["subject"], "RE: 0 Gemini Ave")
        send_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)

    def test_thread_reply_retry_blocks_when_conversation_was_manually_continued(self):
        recipient = "bp21harrison+reply@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nThat time works.\n\nThanks",
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-thread-retry",
        }, doc_id="outbox-thread-retry")
        fake_fs = FakeFirestore()
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-thread-1",
            "sentDateTime": "2026-06-26T12:04:00Z",
        }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_thread_row_number", return_value=7), \
             patch.object(email_module, "_get_reply_message_sender", return_value=recipient), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={
                 "conversationId": "conv-thread-1",
                 "subject": "RE: 0 Gemini Ave",
             }), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None, create=True), \
             patch.object(email_module, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation, create=True) as continuation_guard, \
             patch.object(email_module, "_send_outbox_as_reply") as send_reply:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        continuation_guard.assert_called_once()
        self.assertEqual(continuation_guard.call_args.kwargs["conversation_id"], "conv-thread-1")
        send_reply.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("manually continued", dead_letter_payload["failureReason"])
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "dead_lettered")
        self.assertIn("manually continued", audit_payload["failureReason"])

    def test_retry_guard_lookup_failure_dead_letters_without_resending(self):
        recipient = "bp21harrison+leaguecity-row20@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi Ron,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "0 Gemini Ave, Houston",
            "rowNumber": 20,
            "attempts": 1,
            "lastError": "HTTPSConnectionPool read timed out",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "actionAuditId": "audit-guard-failed",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-guard-failed")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(
                 email_module,
                 "find_matching_sent_message_for_retry",
                 side_effect=email_module.SentMailGuardLookupError("Graph 401"),
             ), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        send_and_index_email.assert_not_called()
        self.assertTrue(doc.reference.deleted)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "dead_lettered")
        self.assertIn("Sent Items retry guard could not verify prior send", dead_letter_payload["failureReason"])

    def test_partial_send_retry_keeps_only_failed_recipients(self):
        doc = FakeDoc({
            "assignedEmails": [
                "bp21harrison+sent@gmail.com",
                "bp21harrison+failed@gmail.com",
            ],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Partial Send Way",
            "rowNumber": 21,
            "attempts": 0,
            "actionAuditId": "audit-partial",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-partial")
        fake_fs = FakeFirestore()

        def send_result(_user_id, _headers, _script, recipients, **_kwargs):
            recipient = recipients[0]
            if recipient == "bp21harrison+sent@gmail.com":
                return {
                    "sent": [recipient],
                    "errors": {},
                    "sentMessageIds": {recipient: "graph-sent-1"},
                }
            return {
                "sent": [],
                "errors": {recipient: "Graph send failed"},
            }

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "send_and_index_email", side_effect=send_result):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        retry_payload = doc.reference.set_calls[-1][0][0]
        self.assertEqual(["bp21harrison+failed@gmail.com"], retry_payload["assignedEmails"])
        self.assertEqual(["bp21harrison+sent@gmail.com"], retry_payload["sentRecipients"])
        self.assertIn("Graph send failed", retry_payload["lastError"])

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "retrying")
        self.assertEqual(audit_payload["sentRecipients"], ["bp21harrison+sent@gmail.com"])
        self.assertEqual(audit_payload["remainingRecipients"], ["bp21harrison+failed@gmail.com"])

    def test_graph_accepted_unindexed_outbox_moves_to_reconciliation_without_retry(self):
        recipient = "bp21harrison+unindexed@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Reconciliation Way",
            "rowNumber": 22,
            "attempts": 0,
            "actionAuditId": "audit-reconciliation",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-reconciliation")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [],
                 "errors": {recipient: "CRITICAL: Failed to index message ID after 3 attempts"},
                 "sentMessageIds": {recipient: "graph-accepted-1"},
                 "internetMessageIds": {recipient: "<accepted-1@example.com>"},
                 "threadIds": {recipient: "accepted-thread-1"},
                 "conversationIds": {recipient: "conversation-1"},
             }):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertTrue(doc.reference.deleted)
        self.assertEqual([], doc.reference.set_calls)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["assignedEmails"], [recipient])
        self.assertEqual(dead_letter_payload["sentRecipients"], [recipient])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "graph-accepted-1")

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "needs_reconciliation")
        self.assertTrue(audit_payload["alreadySent"])
        self.assertEqual(audit_payload["sentRecipients"], [recipient])
        self.assertEqual(audit_payload["sentMessageIds"][recipient], "graph-accepted-1")

    def test_partial_retry_success_unions_prior_sent_and_clears_partial_audit_state(self):
        doc = FakeDoc({
            "assignedEmails": ["bp21harrison+failed@gmail.com"],
            "sentRecipients": ["bp21harrison+sent@gmail.com"],
            "partialSend": True,
            "remainingRecipients": ["bp21harrison+failed@gmail.com"],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Partial Send Way",
            "rowNumber": 21,
            "attempts": 1,
            "actionAuditId": "audit-partial",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-partial")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_select_script_for_recipient", return_value=doc.to_dict()["script"]), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=None), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": ["bp21harrison+failed@gmail.com"],
                 "errors": {},
                 "sentMessageIds": {"bp21harrison+failed@gmail.com": "graph-sent-2"},
                 "internetMessageIds": {"bp21harrison+failed@gmail.com": "<sent-2@example.com>"},
             }):
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
            )

        self.assertTrue(doc.reference.deleted)
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "sent")
        self.assertEqual(
            audit_payload["sentRecipients"],
            ["bp21harrison+sent@gmail.com", "bp21harrison+failed@gmail.com"],
        )
        self.assertEqual(audit_payload["remainingRecipients"], [])
        self.assertFalse(audit_payload["partialSend"])

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

    def test_grouped_graph_accepted_unindexed_outbox_moves_to_reconciliation_without_retry(self):
        recipient = "bp21harrison+grouped@gmail.com"
        doc = FakeDoc({
            "assignedEmails": [recipient],
            "script": "Hi,\n\nCan you share details?\n\nThanks",
            "clientId": "client-1",
            "subject": "123 Grouped Reconciliation Way",
            "rowNumber": 23,
            "attempts": 0,
            "actionAuditId": "audit-grouped-reconciliation",
            "followUpConfig": {"enabled": False},
        }, doc_id="outbox-grouped-reconciliation")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "send_and_index_email", return_value={
                 "sent": [],
                 "errors": {recipient: "CRITICAL: Failed to index message ID after 3 attempts"},
                 "sentMessageIds": {recipient: "graph-grouped-accepted-1"},
                 "internetMessageIds": {recipient: "<grouped-accepted-1@example.com>"},
                 "threadIds": {recipient: "grouped-thread-1"},
                 "conversationIds": {recipient: "grouped-conversation-1"},
             }):
            email_module._send_multi_property_email(
                "uid-1",
                {"Authorization": "Bearer token"},
                recipient,
                [{"doc": doc, "data": doc.to_dict()}],
            )

        self.assertTrue(doc.reference.deleted)
        self.assertEqual([], doc.reference.set_calls)
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual(dead_letter_payload["status"], "needs_reconciliation")
        self.assertTrue(dead_letter_payload["alreadySent"])
        self.assertEqual(dead_letter_payload["assignedEmails"], [recipient])
        self.assertEqual(dead_letter_payload["sentMessageIds"][recipient], "graph-grouped-accepted-1")

        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual(audit_payload["status"], "needs_reconciliation")
        self.assertTrue(audit_payload["alreadySent"])
        self.assertEqual(audit_payload["sentRecipients"], [recipient])


if __name__ == "__main__":
    unittest.main()
