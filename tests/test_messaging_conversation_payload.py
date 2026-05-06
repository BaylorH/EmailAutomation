import unittest
import os
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import messaging


class MessagingConversationPayloadTests(unittest.TestCase):
    def test_build_conversation_payload_tolerates_string_body_messages(self):
        mixed_history = [
            {
                "id": "initial-outbound",
                "data": {
                    "direction": "outbound",
                    "from": "me",
                    "to": ["bp21harrison@gmail.com"],
                    "subject": "3660 N 5th St",
                    "sentDateTime": "2026-05-06T16:49:01Z",
                    "body": {"content": "Could you send specs?", "preview": "Could you send specs?"},
                },
            },
            {
                "id": "dashboard-reply-1",
                "data": {
                    "direction": "outbound",
                    "from": "me",
                    "to": ["bp21harrison@gmail.com"],
                    "subject": "RE: 3660 N 5th St",
                    "sentDateTime": "2026-05-06T18:00:48Z",
                    "body": "The tenant is confidential for now.",
                    "bodyPreview": "The tenant is confidential for now.",
                },
            },
            {
                "id": "broker-specs",
                "data": {
                    "direction": "inbound",
                    "from": "bp21harrison@gmail.com",
                    "to": ["me"],
                    "subject": "Re: 3660 N 5th St",
                    "receivedDateTime": "2026-05-06T18:05:15Z",
                    "body": {
                        "content": "Understood. 14,267 SF is available with 5 docks, 8 drive-ins, and 20' clear.",
                        "preview": "Understood. 14,267 SF is available...",
                    },
                },
            },
        ]

        with patch.object(messaging, "_get_thread_messages_chronological", return_value=mixed_history):
            payload = messaging.build_conversation_payload("uid-1", "thread-1")

        self.assertEqual(len(payload), 3)
        self.assertEqual(payload[1]["content"], "The tenant is confidential for now.")
        self.assertIn("14,267 SF", payload[2]["content"])


if __name__ == "__main__":
    unittest.main()
