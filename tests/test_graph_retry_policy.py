import os
import unittest
from unittest.mock import patch

import requests

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import email as email_module
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

    def test_dashboard_thread_reply_uses_send_retry_budget(self):
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
        self.assertGreaterEqual(retry_calls[0].get("max_retries", 0), 6)


if __name__ == "__main__":
    unittest.main()
