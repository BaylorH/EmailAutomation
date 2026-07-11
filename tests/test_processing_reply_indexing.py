import os
import unittest
from contextvars import copy_context
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("SITESIFT_AUTO_REPLY_ALLOWLIST", "*")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import processing
from email_automation.campaign_safety import CampaignAutomationDecision


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"Unexpected HTTP status {self.status_code}")


class FakeSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return self._data


class FakeFirestore:
    def __init__(self, path=()):
        self.path = path

    def collection(self, name):
        return FakeFirestore(self.path + (name,))

    def document(self, doc_id):
        return FakeFirestore(self.path + (doc_id,))

    def get(self):
        if self.path == ("systemConfig", "campaignAccess"):
            return FakeSnapshot({"automationEnabled": True, "allowedUids": []})
        if len(self.path) == 2 and self.path[0] == "users":
            return FakeSnapshot({"email": "baylor.freelance@outlook.com"})
        if len(self.path) == 4 and self.path[2] == "threads":
            return FakeSnapshot({"clientId": "client-1", "status": "active"})
        if len(self.path) == 4 and self.path[2] == "clients":
            return FakeSnapshot({"status": "live", "automationPaused": False})
        return FakeSnapshot(exists=False)


class ProcessingReplyIndexingTests(unittest.TestCase):
    def test_other_context_terminal_outcome_cannot_suppress_current_retry(self):
        terminal = CampaignAutomationDecision(
            state="blocked",
            reason="other_campaign_stopped",
            client_data={},
            metadata={"terminal": True},
        )
        self.addCleanup(processing._reset_reply_send_outcome)
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="current request failed",
            outcome="send_failed",
            sent_but_unindexed=False,
        )
        current_context = copy_context()
        other_context = copy_context()
        other_context.run(processing._set_reply_campaign_suppression, terminal)

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as reconcile:
            outcome = current_context.run(
                processing._queue_response_retry_or_reconciliation,
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
            )

        self.assertEqual("queued_retry", outcome)
        queue_retry.assert_called_once()
        reconcile.assert_not_called()

    @patch.object(processing.time, "sleep", return_value=None)
    @patch.object(processing.requests, "get")
    def test_sent_reply_lookup_skips_older_conversation_messages(self, requests_get, _sleep):
        requests_get.return_value = FakeResponse(200, {
            "value": [
                {
                    "id": "original-outreach",
                    "internetMessageId": "<original@example.com>",
                    "conversationId": "conversation-1",
                    "sentDateTime": "2026-06-09T18:53:28Z",
                    "bodyPreview": "Original outreach",
                },
                {
                    "id": "closing-reply",
                    "internetMessageId": "<closing@example.com>",
                    "conversationId": "conversation-1",
                    "sentDateTime": "2026-06-09T19:09:27Z",
                    "bodyPreview": "Perfect, thank you",
                },
            ]
        })

        sent = processing._find_recent_sent_message_for_conversation(
            {"Authorization": "Bearer token"},
            "https://graph.microsoft.com/v1.0",
            "conversation-1",
            datetime(2026, 6, 9, 19, 9, 0, tzinfo=timezone.utc),
            attempts=1,
        )

        self.assertEqual("closing-reply", sent["id"])
        self.assertEqual("<closing@example.com>", sent["internetMessageId"])
        self.assertIn("sentDateTime ge 2026-06-09T19:09:00Z", requests_get.call_args.kwargs["params"]["$filter"])

    def test_sent_but_unindexed_auto_response_is_not_queued_for_retry(self):
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="Failed to index reply after 3 attempts",
            sent_but_unindexed=True,
            outcome="sent_but_unindexed",
        )

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as record_reconciliation:
            outcome = processing._queue_response_retry_or_reconciliation(
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
                source_context="autoResponse",
            )

        self.assertEqual("sent_unindexed", outcome)
        queue_retry.assert_not_called()
        record_reconciliation.assert_called_once_with(
            "uid-1",
            "thread-1",
            "msg-1",
            "bp21harrison@gmail.com",
            "Hi,\n\nThanks.",
            "client-1",
            "Failed to index reply after 3 attempts",
            source_context="autoResponse",
        )

    def test_sent_but_unindexed_outcome_counts_as_response_attempted(self):
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="Failed to index reply after 3 attempts",
            sent_but_unindexed=True,
            outcome="sent_but_unindexed",
        )

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as record_reconciliation:
            attempted = processing._handle_auto_response_send_failure(
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
                failure_label="thank you email",
            )

        self.assertTrue(attempted)
        queue_retry.assert_not_called()
        record_reconciliation.assert_called_once()

    def test_auto_thread_reply_preserves_safe_cc_with_reply_all_draft(self):
        posts = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                })
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [
                {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
            ],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", return_value=None):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertFalse(any(url.endswith("/reply") for url in posts))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["baylor@manifoldengineering.ai"],
        )
        self.assertEqual(saved_messages[0][3]["cc"], ["baylor@manifoldengineering.ai"])

    def test_auto_thread_reply_fetches_created_draft_when_graph_omits_recipients(self):
        posts = []
        gets = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "auto-reply-draft-1"})
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                })
            if url.endswith("/me/messages/auto-reply-draft-1"):
                return FakeResponse(200, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [
                {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
            ],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", return_value=None):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/me/messages/auto-reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["baylor@manifoldengineering.ai"],
        )
        self.assertEqual(saved_messages[0][3]["cc"], ["baylor@manifoldengineering.ai"])

    def test_auto_thread_reply_rebuilds_reply_all_from_source_when_draft_stays_empty(self):
        posts = []
        gets = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "auto-reply-draft-1"})
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                    "from": {
                        "emailAddress": {
                            "name": "Avery",
                            "address": "bp21harrison@gmail.com",
                        }
                    },
                    "toRecipients": [
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                    ],
                })
            if url.endswith("/me/messages/auto-reply-draft-1"):
                return FakeResponse(200, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [],
                    "ccRecipients": [],
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        def fake_optout(_user_id, email):
            if email.lower() == "baylor@manifoldengineering.ai":
                return {"reason": "temporary proof opt-out"}
            return None

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", side_effect=fake_optout):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/me/messages/msg-1") for url in gets))
        self.assertTrue(any(url.endswith("/me/messages/auto-reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(patch_payload["ccRecipients"], [])
        self.assertEqual(saved_messages[0][3]["cc"], [])

    def test_auto_thread_reply_falls_back_to_current_recipient_when_graph_stays_empty(self):
        posts = []
        gets = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "auto-reply-draft-1"})
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                })
            if url.endswith("/me/messages/auto-reply-draft-1"):
                return FakeResponse(200, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [],
                    "ccRecipients": [],
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", return_value=None):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/me/messages/msg-1") for url in gets))
        self.assertTrue(any(url.endswith("/me/messages/auto-reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(patch_payload["ccRecipients"], [])
        self.assertEqual(saved_messages[0][3]["to"], ["bp21harrison@gmail.com"])
        self.assertEqual(saved_messages[0][3]["cc"], [])


if __name__ == "__main__":
    unittest.main()
