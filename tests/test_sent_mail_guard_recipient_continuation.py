import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from email_automation.sent_mail_guard import (
    SentMailGuardLookupError,
    find_sent_recipient_continuation_for_retry,
)


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class SentRecipientContinuationTests(unittest.TestCase):
    def test_detects_new_conversation_manual_send_to_same_recipient(self):
        response = _Response(200, {
            "value": [{
                "id": "sent-new-conversation",
                "conversationId": "different-conversation",
                "internetMessageId": "<manual@example.test>",
                "subject": "Quick update",
                "sentDateTime": "2026-07-12T18:05:00Z",
                "toRecipients": [{
                    "emailAddress": {"address": "bp21harrison@gmail.com"}
                }],
            }]
        })

        with patch(
            "email_automation.sent_mail_guard.requests.get",
            return_value=response,
        ) as get:
            match = find_sent_recipient_continuation_for_retry(
                {"Authorization": "Bearer test"},
                recipient="bp21harrison@gmail.com",
                sent_after=datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc),
            )

        self.assertEqual("sent-new-conversation", match["id"])
        selected = get.call_args.kwargs["params"]["$select"]
        self.assertNotIn("body", selected.lower())

    def test_unreadable_sent_items_fails_closed(self):
        with patch(
            "email_automation.sent_mail_guard.requests.get",
            return_value=_Response(503, {}),
        ):
            with self.assertRaises(SentMailGuardLookupError):
                find_sent_recipient_continuation_for_retry(
                    {"Authorization": "Bearer test"},
                    recipient="bp21harrison@gmail.com",
                    sent_after=datetime(2026, 7, 12, 18, 0, tzinfo=timezone.utc),
                    attempts=1,
                )


if __name__ == "__main__":
    unittest.main()
