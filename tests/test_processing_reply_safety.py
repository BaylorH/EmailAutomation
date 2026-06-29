import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import processing


class ProcessingReplySafetyTests(unittest.TestCase):
    def test_send_reply_blocks_non_allowlisted_auto_reply_before_graph_request(self):
        with patch.dict(os.environ, {"SITESIFT_AUTO_REPLY_ALLOWLIST": "NO7lVYVp6BaplKYEfMlWCgBnpdh2"}), patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=AssertionError("Graph should not be touched for non-allowlisted auto-replies"),
        ):
            sent = processing.send_reply_in_thread(
                user_id="regular-user",
                headers={"Authorization": "Bearer token"},
                body="Hi Alex,\n\nThanks for the update.",
                current_msg_id="message-1",
                recipient="broker@example.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual("blocked_auto_reply_policy", processing.send_reply_in_thread.last_outcome)
        self.assertIn("Automatic inbox replies are disabled", processing.send_reply_in_thread.last_error)

    def test_send_reply_blocks_placeholder_before_graph_request(self):
        with patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=AssertionError("Graph should not be touched for unsafe reply bodies"),
        ):
            sent = processing.send_reply_in_thread(
                user_id="uid-1",
                headers={"Authorization": "Bearer token"},
                body="Hi [NAME],\n\nThanks for confirming.",
                current_msg_id="message-1",
                recipient="bp21harrison@gmail.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual("blocked_unsafe_body", processing.send_reply_in_thread.last_outcome)
        self.assertIn("Unresolved outbound placeholder", processing.send_reply_in_thread.last_error)


if __name__ == "__main__":
    unittest.main()
