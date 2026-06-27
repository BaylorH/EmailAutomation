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

        with patch.object(email_module, "exponential_backoff_request", side_effect=fake_retry), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={"conversationId": "conv-1"}), \
             patch.object(email_module, "_find_recent_sent_reply_identity", return_value={"sentMessageId": "sent-1"}), \
             patch.object(email_module.requests, "post", return_value=FakeResponse(202)):
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
