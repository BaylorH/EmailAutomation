import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import requests

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import email as email_module
from email_automation import sent_mail_guard
from email_automation import utils


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} error: {self.text}",
                response=self,
            )


class GraphRetryPolicyTests(unittest.TestCase):
    def test_rate_limit_failure_preserves_graph_error_details(self):
        responses = [
            FakeResponse(
                429,
                text='{"error":{"code":"TooManyRequests","message":"Mailbox send throttled"}}',
                headers={"Retry-After": "0"},
            ),
            FakeResponse(
                429,
                text='{"error":{"code":"TooManyRequests","message":"Mailbox send throttled"}}',
                headers={"Retry-After": "0"},
            ),
        ]

        with patch.object(utils.time, "sleep", return_value=None):
            with self.assertRaises(requests.exceptions.HTTPError) as raised:
                utils.exponential_backoff_request(lambda: responses.pop(0), max_retries=2)

        self.assertIn("TooManyRequests", str(raised.exception))
        self.assertIn("Mailbox send throttled", str(raised.exception))

    def test_dashboard_thread_reply_send_endpoint_is_not_auto_retried(self):
        retry_calls = []

        def fake_retry(func, **kwargs):
            retry_calls.append(kwargs)
            return func()

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
            return FakeResponse(500, text=f"Unexpected POST {url}")

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={"conversationId": "conv-1"}), \
             patch.object(email_module, "_find_recent_sent_reply_identity", return_value={"sentMessageId": "sent-1"}), \
             patch.object(email_module.requests, "post", side_effect=fake_post), \
             patch.object(email_module.requests, "patch", return_value=FakeResponse(200)), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "reply-message-1",
                "thread-1",
            )

        self.assertTrue(result["sent"])
        send_calls = [
            call for call in retry_calls
            if call.get("operation") == "graph_send"
        ]
        self.assertTrue(send_calls)
        self.assertEqual(send_calls[-1].get("max_retries"), 1)

    def test_dashboard_thread_reply_preserves_safe_cc_with_reply_all_draft(self):
        posts = []
        patch_payloads = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                })
            if url.endswith("/reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500, text=f"Unexpected POST {url}")

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={"conversationId": "conv-1"}), \
             patch.object(email_module, "_find_recent_sent_reply_identity", return_value={"sentMessageId": "sent-1"}), \
             patch.object(email_module.requests, "post", side_effect=fake_post), \
             patch.object(email_module.requests, "patch", side_effect=fake_patch), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "reply-message-1",
                "thread-1",
                user_email="baylor.freelance@outlook.com",
            )

        self.assertTrue(result["sent"])
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

    def test_dashboard_thread_reply_fetches_created_draft_when_graph_omits_recipients(self):
        posts = []
        gets = []
        patch_payloads = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "reply-draft-1"})
            if url.endswith("/reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500, text=f"Unexpected POST {url}")

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/reply-draft-1"):
                return FakeResponse(200, {
                    "id": "reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                })
            return FakeResponse(404, text=f"Unexpected GET {url}")

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={"conversationId": "conv-1"}), \
             patch.object(email_module, "_find_recent_sent_reply_identity", return_value={"sentMessageId": "sent-1"}), \
             patch.object(email_module.requests, "post", side_effect=fake_post), \
             patch.object(email_module.requests, "get", side_effect=fake_get), \
             patch.object(email_module.requests, "patch", side_effect=fake_patch), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "reply-message-1",
                "thread-1",
                user_email="baylor.freelance@outlook.com",
            )

        self.assertTrue(result["sent"])
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["baylor@manifoldengineering.ai"],
        )

    def test_dashboard_thread_reply_rebuilds_reply_all_from_source_when_draft_stays_empty(self):
        posts = []
        gets = []
        patch_payloads = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "reply-draft-1"})
            if url.endswith("/reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500, text=f"Unexpected POST {url}")

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/reply-message-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 410 Genesis Blvd, Webster",
                    "from": {
                        "emailAddress": {
                            "name": "Jason",
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
            if url.endswith("/reply-draft-1"):
                return FakeResponse(200, {
                    "id": "reply-draft-1",
                    "toRecipients": [],
                    "ccRecipients": [],
                })
            return FakeResponse(404, text=f"Unexpected GET {url}")

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        def fake_optout(_user_id, email):
            if email.lower() == "baylor@manifoldengineering.ai":
                return {"reason": "temporary proof opt-out"}
            return None

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module, "_find_recent_sent_reply_identity", return_value={"sentMessageId": "sent-1"}), \
             patch.object(email_module.requests, "post", side_effect=fake_post), \
             patch.object(email_module.requests, "get", side_effect=fake_get), \
             patch.object(email_module.requests, "patch", side_effect=fake_patch), \
             patch("email_automation.processing.is_contact_opted_out", side_effect=fake_optout):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Jason,\n\nThanks.",
                "reply-message-1",
                "thread-1",
                user_email="baylor.freelance@outlook.com",
            )

        self.assertTrue(result["sent"])
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/reply-message-1") for url in gets))
        self.assertTrue(any(url.endswith("/reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(patch_payload["ccRecipients"], [])
        self.assertEqual(result["skippedRecipients"]["optedOut"][0]["email"], "baylor@manifoldengineering.ai")

    def test_dashboard_thread_reply_falls_back_to_reviewed_outbox_recipients_when_graph_stays_empty(self):
        posts = []
        gets = []
        patch_payloads = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "reply-draft-1"})
            if url.endswith("/reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500, text=f"Unexpected POST {url}")

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/reply-message-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 410 Genesis Blvd, Webster",
                })
            if url.endswith("/reply-draft-1"):
                return FakeResponse(200, {
                    "id": "reply-draft-1",
                    "toRecipients": [],
                    "ccRecipients": [],
                })
            return FakeResponse(404, text=f"Unexpected GET {url}")

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module, "_find_recent_sent_reply_identity", return_value={"sentMessageId": "sent-1"}), \
             patch.object(email_module.requests, "post", side_effect=fake_post), \
             patch.object(email_module.requests, "get", side_effect=fake_get), \
             patch.object(email_module.requests, "patch", side_effect=fake_patch), \
             patch("email_automation.processing.is_contact_opted_out", return_value=None):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Jason,\n\nThanks.",
                "reply-message-1",
                "thread-1",
                user_email="baylor.freelance@outlook.com",
                fallback_to_emails=["bp21harrison@gmail.com"],
                fallback_cc_emails=["baylor@manifoldengineering.ai"],
            )

        self.assertTrue(result["sent"])
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/reply-message-1") for url in gets))
        self.assertTrue(any(url.endswith("/reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["baylor@manifoldengineering.ai"],
        )
        self.assertEqual(result["toRecipients"], ["bp21harrison@gmail.com"])
        self.assertEqual(result["ccRecipients"], ["baylor@manifoldengineering.ai"])

    def test_reply_all_source_fallback_accepts_stored_envelope_address_lists(self):
        draft = {"id": "reply-draft-1", "toRecipients": [], "ccRecipients": []}
        stored_envelope = {
            "from": "bp21harrison@gmail.com",
            "to": ["baylor.freelance@outlook.com"],
            "cc": ["baylor@manifoldengineering.ai"],
        }

        rebuilt = email_module._source_message_reply_all_fallback(draft, stored_envelope)

        with patch("email_automation.processing.is_contact_opted_out", return_value=None):
            recipient_result = email_module._filter_reply_all_draft_recipients(
                "uid-1",
                rebuilt,
                user_email="baylor.freelance@outlook.com",
            )

        payload = recipient_result["payload"]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in payload["ccRecipients"]],
            ["baylor@manifoldengineering.ai"],
        )

    def test_dashboard_thread_reply_filters_opted_out_cc_before_send(self):
        patch_payloads = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                    ],
                })
            if url.endswith("/reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500, text=f"Unexpected POST {url}")

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        def fake_optout(_user_id, email):
            if email.lower() == "baylor@manifoldengineering.ai":
                return {"reason": "unsubscribe"}
            return None

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={"conversationId": "conv-1"}), \
             patch.object(email_module, "_find_recent_sent_reply_identity", return_value={"sentMessageId": "sent-1"}), \
             patch.object(email_module.requests, "post", side_effect=fake_post), \
             patch.object(email_module.requests, "patch", side_effect=fake_patch), \
             patch("email_automation.processing.is_contact_opted_out", side_effect=fake_optout):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "reply-message-1",
                "thread-1",
                user_email="baylor.freelance@outlook.com",
            )

        self.assertTrue(result["sent"])
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(patch_payload["ccRecipients"], [])

    def test_dashboard_thread_reply_deletes_created_draft_when_all_recipients_filtered(self):
        deleted_urls = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                    ],
                })
            return FakeResponse(500, text=f"Unexpected POST {url}")

        def fake_delete(url, **_kwargs):
            deleted_urls.append(url)
            return FakeResponse(204)

        def fake_optout(_user_id, email):
            if email.lower() == "baylor@manifoldengineering.ai":
                return {"reason": "unsubscribe"}
            return None

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module.requests, "post", side_effect=fake_post), \
             patch.object(email_module.requests, "delete", side_effect=fake_delete), \
             patch("email_automation.processing.is_contact_opted_out", side_effect=fake_optout):
            result = email_module._send_outbox_as_reply(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "reply-message-1",
                "thread-1",
                user_email="baylor.freelance@outlook.com",
            )

        self.assertFalse(result["sent"])
        self.assertEqual(result["error"], "Reply-all draft has no safe recipients after filtering")
        self.assertTrue(any(url.endswith("/reply-draft-1") for url in deleted_urls))

    def test_sent_items_retry_guard_matches_recipient_subject_body_after_cutoff(self):
        messages = [
            {
                "id": "wrong-recipient",
                "internetMessageId": "<wrong-recipient@example.com>",
                "conversationId": "conv-1",
                "subject": "0 Gemini Ave, Houston",
                "sentDateTime": "2026-06-26T12:03:00Z",
                "toRecipients": [{"emailAddress": {"address": "someone-else@example.com"}}],
                "body": {"content": "Hi Ron, Can you share details?"},
                "bodyPreview": "Hi Ron, Can you share details?",
            },
            {
                "id": "wrong-body",
                "internetMessageId": "<wrong-body@example.com>",
                "conversationId": "conv-1",
                "subject": "0 Gemini Ave, Houston",
                "sentDateTime": "2026-06-26T12:04:00Z",
                "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                "body": {"content": "A totally different message"},
                "bodyPreview": "A totally different message",
            },
            {
                "id": "wrong-subject",
                "internetMessageId": "<wrong-subject@example.com>",
                "conversationId": "conv-1",
                "subject": "RE: Completely different property",
                "sentDateTime": "2026-06-26T12:04:30Z",
                "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                "body": {"content": "<p>Hi Ron,</p><p>Can you share details?</p><p>Thanks</p>"},
                "bodyPreview": "Hi Ron, Can you share details? Thanks",
            },
            {
                "id": "valid",
                "internetMessageId": "<valid@example.com>",
                "conversationId": "conv-1",
                "subject": "RE: 0 Gemini Ave, Houston",
                "sentDateTime": "2026-06-26T12:05:00Z",
                "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                "body": {"content": "<p>Hi Ron,</p><p>Can you share details?</p><p>Thanks</p>"},
                "bodyPreview": "Hi Ron, Can you share details? Thanks",
            },
        ]
        captured = {}

        def fake_get(_url, **kwargs):
            captured.update(kwargs)
            return FakeResponse(200, {"value": messages})

        with patch.object(sent_mail_guard.requests, "get", side_effect=fake_get):
            match = sent_mail_guard.find_matching_sent_message_for_retry(
                {"Authorization": "Bearer token"},
                recipient="bp21harrison@gmail.com",
                body="Hi Ron,\n\nCan you share details?\n\nThanks",
                subject="0 Gemini Ave, Houston",
                sent_after=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(match["id"], "valid")
        self.assertIn("sentDateTime ge 2026-06-26T12:00:00Z", captured["params"]["$filter"])

    def test_manual_continuation_guard_uses_metadata_only(self):
        messages = [
            {
                "id": "old-sent",
                "internetMessageId": "<old-sent@example.com>",
                "conversationId": "conv-1",
                "subject": "RE: 0 Gemini Ave, Houston",
                "sentDateTime": "2026-06-26T11:59:00Z",
                "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
            },
            {
                "id": "other-conversation",
                "internetMessageId": "<other-conversation@example.com>",
                "conversationId": "conv-2",
                "subject": "RE: 0 Gemini Ave, Houston",
                "sentDateTime": "2026-06-26T12:10:00Z",
                "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
            },
            {
                "id": "manual-continuation",
                "internetMessageId": "<manual-continuation@example.com>",
                "conversationId": "conv-1",
                "subject": "RE: 0 Gemini Ave, Houston",
                "sentDateTime": "2026-06-26T12:11:00Z",
                "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
            },
        ]
        captured = {}

        def fake_get(_url, **kwargs):
            captured.update(kwargs)
            return FakeResponse(200, {"value": messages})

        with patch.object(sent_mail_guard.requests, "get", side_effect=fake_get):
            match = sent_mail_guard.find_sent_conversation_continuation_for_retry(
                {"Authorization": "Bearer token"},
                conversation_id="conv-1",
                sent_after=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(match["id"], "manual-continuation")
        self.assertEqual(match["internetMessageId"], "<manual-continuation@example.com>")
        selected_fields = captured["params"]["$select"]
        self.assertNotIn("body", selected_fields)
        self.assertNotIn("bodyPreview", selected_fields)
        self.assertIn("sentDateTime ge 2026-06-26T12:00:00Z", captured["params"]["$filter"])

    def test_sent_items_retry_guard_rejects_short_subset_body_match(self):
        messages = [
            {
                "id": "too-short",
                "internetMessageId": "<too-short@example.com>",
                "conversationId": "conv-1",
                "subject": "RE: 0 Gemini Ave, Houston",
                "sentDateTime": "2026-06-26T12:03:00Z",
                "toRecipients": [{"emailAddress": {"address": "bp21harrison@gmail.com"}}],
                "body": {"content": "Hi Ron"},
                "bodyPreview": "Hi Ron",
            }
        ]

        with patch.object(sent_mail_guard.requests, "get", return_value=FakeResponse(200, {"value": messages})):
            match = sent_mail_guard.find_matching_sent_message_for_retry(
                {"Authorization": "Bearer token"},
                recipient="bp21harrison@gmail.com",
                body="Hi Ron,\n\nCan you share details for the 0 Gemini property?\n\nThanks",
                subject="0 Gemini Ave, Houston",
                sent_after=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc),
            )

        self.assertIsNone(match)

    def test_sent_items_retry_guard_fails_closed_for_short_reply_without_thread_identity(self):
        with self.assertRaises(sent_mail_guard.SentMailGuardLookupError) as raised:
            sent_mail_guard.find_matching_sent_message_for_retry(
                {"Authorization": "Bearer token"},
                recipient="bp21harrison@gmail.com",
                body="Thanks, please send it.",
                sent_after=datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc),
            )

        self.assertIn("not enough unique message identity", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
