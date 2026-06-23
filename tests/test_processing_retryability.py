import unittest
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import processing


class ProcessingRetryabilityTests(unittest.TestCase):
    def test_retryable_ai_failures_do_not_mark_messages_processed(self):
        self.assertFalse(
            processing._should_mark_processed_after_error(
                processing.RetryableProcessingError("AI proposal unavailable")
            )
        )
        self.assertTrue(processing._should_mark_processed_after_error(ValueError("non-retryable bug")))

    def test_successful_retry_can_clear_matching_processing_failure(self):
        fake_fs = MagicMock()

        with patch.object(processing, "_fs", fake_fs):
            processing._clear_ai_processing_failure("uid-1", "thread-1", "message-1")

        fake_fs.collection.assert_called_once_with("users")
        fake_fs.collection.return_value.document.assert_called_once_with("uid-1")
        failures_collection = fake_fs.collection.return_value.document.return_value.collection
        failures_collection.assert_called_once_with("processingFailures")
        failure_doc = failures_collection.return_value.document
        failure_doc.assert_called_once_with("thread-1__message-1")
        failure_doc.return_value.delete.assert_called_once()

    def test_clear_processing_failure_ignores_missing_message_id(self):
        fake_fs = MagicMock()

        with patch.object(processing, "_fs", fake_fs):
            processing._clear_ai_processing_failure("uid-1", "thread-1", "")

        fake_fs.collection.assert_not_called()

    def test_new_property_duplicate_check_fails_open_on_sheet_read_error(self):
        class FailingSheets:
            def spreadsheets(self):
                return self

            def values(self):
                return self

            def get(self, **_kwargs):
                return self

            def execute(self):
                raise RuntimeError("sheets quota")

        header = ["Property Address", "City"]

        self.assertFalse(
            processing._property_exists_in_sheet(
                FailingSheets(),
                "sheet-1",
                "Properties",
                header,
                "777 Replacement Signal Ave",
                "Las Vegas",
            )
        )


if __name__ == "__main__":
    unittest.main()
