import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import processing


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"Unexpected HTTP status {self.status_code}")


class ProcessingReplyIndexingTests(unittest.TestCase):
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
        processing.send_reply_in_thread.last_error = "Failed to index reply after 3 attempts"
        processing.send_reply_in_thread.sent_but_unindexed = True

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
        processing.send_reply_in_thread.last_error = "Failed to index reply after 3 attempts"
        processing.send_reply_in_thread.sent_but_unindexed = True

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


if __name__ == "__main__":
    unittest.main()
