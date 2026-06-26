import unittest
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub.nosync/EmailAutomation/service-account.json",
)

from email_automation import processing


class ProcessingRetryabilityTests(unittest.TestCase):
    def test_retryable_ai_failures_do_not_mark_messages_processed(self):
        self.assertFalse(
            processing._should_mark_processed_after_error(
                processing.RetryableProcessingError("AI proposal unavailable")
            )
        )
        self.assertFalse(processing._should_mark_processed_after_error(ValueError("unexpected bug")))
        self.assertTrue(processing._should_mark_processed_after_error(None))

    def test_scan_records_unexpected_processing_crash_without_marking_processed(self):
        response = MagicMock()
        received_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        response.json.return_value = {
            "value": [
                {
                    "id": "graph-message-1",
                    "internetMessageId": "<message-1@example.test>",
                    "subject": "RE: 4402 Rex Rd",
                    "receivedDateTime": received_now,
                }
            ]
        }

        with patch.object(processing, "exponential_backoff_request", return_value=response), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_match_message_to_thread", return_value="thread-1"), \
             patch.object(processing, "process_inbox_message", side_effect=ValueError("flyer_links crash")), \
             patch.object(processing, "_record_ai_processing_failure") as record_failure, \
             patch.object(processing, "mark_processed") as mark_processed, \
             patch.object(processing, "set_last_scan_iso"):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        record_failure.assert_called_once_with(
            "uid-1",
            "unknown",
            "thread-1",
            "<message-1@example.test>",
            "flyer_links crash",
        )
        mark_processed.assert_not_called()
        self.assertEqual(0, result["processed"])

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

    def test_reconcile_processing_failures_clears_processed_messages_only(self):
        processed_doc = MagicMock()
        processed_doc.id = "failure-processed"
        processed_doc.to_dict.return_value = {
            "threadId": "thread-1",
            "messageId": "message-processed",
            "retryable": True,
        }
        retry_doc = MagicMock()
        retry_doc.id = "failure-retry"
        retry_doc.to_dict.return_value = {
            "threadId": "thread-2",
            "messageId": "message-retry",
            "retryable": True,
        }
        missing_id_doc = MagicMock()
        missing_id_doc.id = "failure-missing"
        missing_id_doc.to_dict.return_value = {
            "threadId": "thread-3",
            "retryable": True,
        }

        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [
            processed_doc,
            retry_doc,
            missing_id_doc,
        ]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection

        def fake_has_processed(_user_id, message_id):
            return message_id == "message-processed"

        with patch.object(processing, "_fs", fake_fs), patch.object(processing, "has_processed", side_effect=fake_has_processed):
            result = processing.reconcile_stale_processing_failures("uid-1")

        self.assertEqual({"checked": 3, "cleared": 1, "retained": 2}, result)
        processed_doc.reference.delete.assert_called_once()
        retry_doc.reference.delete.assert_not_called()
        missing_id_doc.reference.delete.assert_not_called()

    def test_retry_processing_failures_processes_exact_graph_message_and_clears_success(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 1,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection
        graph_response = MagicMock()
        graph_response.json.return_value = {
            "id": "message-1",
            "subject": "RE: 16 Jupiter Ln",
            "internetMessageId": "<message-1@example.test>",
            "conversationId": "conversation-1",
        }

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request", return_value=graph_response), \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures("uid-1", {"Authorization": "Bearer fake"})

        self.assertEqual(
            {"checked": 1, "retried": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            result,
        )
        process_message.assert_called_once()
        mark_processed.assert_any_call("uid-1", "message-1")
        mark_processed.assert_any_call("uid-1", "<message-1@example.test>")
        self.assertEqual(2, mark_processed.call_count)
        failure_doc.reference.delete.assert_called_once()

    def test_retry_processing_failures_keeps_retryable_message_visible_on_retry_error(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 1,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection
        graph_response = MagicMock()
        graph_response.json.return_value = {
            "id": "message-1",
            "internetMessageId": "<message-1@example.test>",
        }

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request", return_value=graph_response), \
             patch.object(processing, "process_inbox_message", side_effect=processing.RetryableProcessingError("still failing")), \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures("uid-1", {"Authorization": "Bearer fake"}, max_attempts=3)

        self.assertEqual(
            {"checked": 1, "retried": 1, "succeeded": 0, "failed": 1, "skipped": 0},
            result,
        )
        mark_processed.assert_not_called()
        failure_doc.reference.delete.assert_not_called()
        failure_doc.reference.set.assert_called_once()
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertEqual(2, update_payload["processingAttempts"])
        self.assertTrue(update_payload["retryable"])
        self.assertIn("still failing", update_payload["lastRetryError"])

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
