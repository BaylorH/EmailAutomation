import unittest
import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import processing


class TerminalThreadProcessingTests(unittest.TestCase):
    def test_completed_threads_are_terminal_for_inbox_processing(self):
        self.assertTrue(
            processing._should_skip_processing_for_terminal_thread(processing.THREAD_STATUS["completed"])
        )

    def test_stopped_threads_are_terminal_for_inbox_processing(self):
        self.assertTrue(
            processing._should_skip_processing_for_terminal_thread(processing.THREAD_STATUS["stopped"])
        )

    def test_active_and_paused_threads_still_process(self):
        self.assertFalse(
            processing._should_skip_processing_for_terminal_thread(processing.THREAD_STATUS["active"])
        )
        self.assertFalse(
            processing._should_skip_processing_for_terminal_thread(processing.THREAD_STATUS["paused"])
        )
        self.assertFalse(processing._should_skip_processing_for_terminal_thread(None))


if __name__ == "__main__":
    unittest.main()
