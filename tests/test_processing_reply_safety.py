import os
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import processing


class ProcessingReplySafetyTests(unittest.TestCase):
    def test_processing_does_not_import_legacy_email_operations_senders(self):
        source = Path(processing.__file__).read_text()

        self.assertNotIn("from .email_operations import", source)

    def test_send_reply_default_allowlist_is_baylor_only(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "email_automation.utils.exponential_backoff_request",
            side_effect=AssertionError("Graph should not be touched for non-Baylor default auto-replies"),
        ):
            sent = processing.send_reply_in_thread(
                user_id="C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
                headers={"Authorization": "Bearer token"},
                body="Hi Alex,\n\nThanks for the update.",
                current_msg_id="message-1",
                recipient="broker@example.com",
                thread_id="thread-1",
            )

        self.assertFalse(sent)
        self.assertEqual("blocked_auto_reply_policy", processing.send_reply_in_thread.last_outcome)

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

    def test_tour_actions_default_allowlist_is_baylor_only(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(processing._tour_actions_allowed("NO7lVYVp6BaplKYEfMlWCgBnpdh2"))
            self.assertFalse(processing._tour_actions_allowed("ntR8ACrAgEcZ1i5FWyi6MFuCJfI2"))

    def test_tour_actions_explicit_allowlist_supports_test_lane(self):
        with patch.dict(os.environ, {"SITESIFT_TOUR_ACTION_ALLOWLIST": "test-user, other-user"}):
            self.assertTrue(processing._tour_actions_allowed("test-user"))
            self.assertFalse(processing._tour_actions_allowed("regular-user"))

    def test_tour_actions_wildcard_is_explicit_only(self):
        with patch.dict(os.environ, {"SITESIFT_TOUR_ACTION_ALLOWLIST": "*"}):
            self.assertTrue(processing._tour_actions_allowed("regular-user"))


if __name__ == "__main__":
    unittest.main()
