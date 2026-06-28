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

    def test_stopped_original_thread_processes_active_replacement_reply(self):
        thread_data = {
            "status": processing.THREAD_STATUS["stopped"],
            "activeReplacementProperty": {
                "address": "414 Alternate Signal Pkwy",
                "city": "North Las Vegas",
                "rowNumber": 7,
            },
        }
        message_text = "Here is the packet information for 414 Alternate Signal Pkwy, North Las Vegas."

        self.assertFalse(
            processing._should_skip_processing_for_terminal_thread(
                processing.THREAD_STATUS["stopped"],
                thread_data=thread_data,
                message_text=message_text,
            )
        )

    def test_stopped_original_thread_still_skips_unrelated_late_reply(self):
        thread_data = {
            "status": processing.THREAD_STATUS["stopped"],
            "activeReplacementProperty": {
                "address": "414 Alternate Signal Pkwy",
                "city": "North Las Vegas",
                "rowNumber": 7,
            },
        }
        message_text = "Thanks, let me know if anything else comes up for 404 Signature Replacement Rd."

        self.assertTrue(
            processing._should_skip_processing_for_terminal_thread(
                processing.THREAD_STATUS["stopped"],
                thread_data=thread_data,
                message_text=message_text,
            )
        )

    def test_replacement_context_ignores_blank_address_without_crashing(self):
        thread_data = {
            "status": processing.THREAD_STATUS["stopped"],
            "activeReplacementProperty": {
                "address": None,
                "propertyAddress": None,
                "rowAnchor": None,
                "city": None,
                "rowNumber": 7,
            },
        }

        self.assertIsNone(
            processing._active_replacement_context(
                thread_data,
                message_text="The alternate property is available.",
            )
        )

    def test_replacement_context_normalizes_non_string_property_fields(self):
        thread_data = {
            "status": processing.THREAD_STATUS["stopped"],
            "activeReplacementProperty": {
                "address": 414,
                "city": 12345,
                "rowNumber": "7",
            },
        }

        context = processing._active_replacement_context(
            thread_data,
            message_text="Here are the details for 414.",
        )

        self.assertEqual("414", context["address"])
        self.assertEqual("12345", context["city"])
        self.assertEqual(7, context["rowNumber"])

    def test_completed_threads_remain_terminal_even_with_replacement_context(self):
        thread_data = {
            "activeReplacementProperty": {
                "address": "414 Alternate Signal Pkwy",
                "rowNumber": 7,
            },
        }

        self.assertTrue(
            processing._should_skip_processing_for_terminal_thread(
                processing.THREAD_STATUS["completed"],
                thread_data=thread_data,
                message_text="414 Alternate Signal Pkwy packet attached.",
            )
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
