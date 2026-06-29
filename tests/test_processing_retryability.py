import unittest
import os
from datetime import datetime, timedelta, timezone
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

    def test_scan_skips_inbox_retry_when_user_manually_continued_conversation(self):
        response = MagicMock()
        received_at_dt = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0)
        manual_sent_at = (received_at_dt + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        received_at = received_at_dt.isoformat().replace("+00:00", "Z")
        response.json.return_value = {
            "value": [
                {
                    "id": "graph-message-1",
                    "internetMessageId": "<message-1@example.test>",
                    "subject": "RE: 4402 Rex Rd",
                    "receivedDateTime": received_at,
                    "conversationId": "conversation-1",
                }
            ]
        }
        manual_continuation = {
            "id": "sent-manual-1",
            "internetMessageId": "<manual-reply@example.test>",
            "conversationId": "conversation-1",
            "sentDateTime": manual_sent_at,
        }

        with patch.object(processing, "exponential_backoff_request", return_value=response), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_match_message_to_thread", return_value="thread-1"), \
             patch.object(processing, "_has_processing_failure_record", return_value=True, create=True), \
             patch.object(processing, "find_sent_conversation_continuation_for_retry", return_value=manual_continuation, create=True) as continuation_guard, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed, \
             patch.object(processing, "_record_processing_failure_blocked_by_manual_continuation", create=True) as record_blocked, \
             patch.object(processing, "set_last_scan_iso"):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        continuation_guard.assert_called_once()
        self.assertEqual("conversation-1", continuation_guard.call_args.kwargs["conversation_id"])
        self.assertEqual(received_at_dt, continuation_guard.call_args.kwargs["sent_after"])
        process_message.assert_not_called()
        record_blocked.assert_called_once_with(
            "uid-1",
            "unknown",
            "thread-1",
            "<message-1@example.test>",
            manual_continuation,
        )
        mark_processed.assert_called_once_with("uid-1", "<message-1@example.test>")
        self.assertEqual(0, result["processed"])
        self.assertEqual(1, result["skipped"])

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

    def test_reconcile_new_property_failure_clears_when_approval_notification_exists(self):
        failure_doc = MagicMock()
        failure_doc.id = "failure-new-property"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "reason": "new_property_event_failed:'NoneType' object has no attribute 'strip'",
            "retryable": False,
            "recoveryStatus": "stale_manual_review",
        }

        notification_doc = MagicMock()
        notification_doc.to_dict.return_value = {
            "kind": "action_needed",
            "threadId": "thread-1",
            "meta": {"reason": "new_property_pending_approval"},
        }

        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        notifications_collection = MagicMock()
        notifications_collection.where.return_value.limit.return_value.stream.return_value = [notification_doc]
        clients_collection = MagicMock()
        clients_collection.document.return_value.collection.return_value = notifications_collection

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "clients": clients_collection,
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc

        with patch.object(processing, "_fs", fake_fs), patch.object(processing, "has_processed", return_value=False):
            result = processing.reconcile_stale_processing_failures("uid-1")

        self.assertEqual({"checked": 1, "cleared": 1, "retained": 0}, result)
        failure_doc.reference.delete.assert_called_once()

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

    def test_retry_processing_failures_skips_stale_failure_without_fetching_graph(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 0,
            "createdAt": datetime.now(timezone.utc) - timedelta(hours=8),
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request") as fetch_graph_message, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
                max_failure_age_hours=6,
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        fetch_graph_message.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        failure_doc.reference.set.assert_called_once()
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertFalse(update_payload["retryable"])
        self.assertEqual("stale_manual_review", update_payload["recoveryStatus"])
        self.assertIn("older than 6 hours", update_payload["lastRetryError"])

    def test_retry_processing_failures_blocks_when_outbox_already_targets_source_message(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 0,
        }
        outbox_doc = MagicMock()
        outbox_doc.id = "outbox-existing"
        outbox_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "message-1",
            "status": "queued",
        }

        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        outbox_collection = MagicMock()
        outbox_collection.limit.return_value.stream.return_value = [outbox_doc]
        empty_collection = MagicMock()
        empty_collection.limit.return_value.stream.return_value = []
        thread_ref = MagicMock()
        thread_ref.get.return_value.exists = False

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "outbox": outbox_collection,
            "pendingResponses": empty_collection,
            "deadLetterQueue": empty_collection,
            "actionAudit": empty_collection,
            "clients": MagicMock(document=MagicMock(return_value=MagicMock(collection=MagicMock(return_value=empty_collection)))),
            "threads": MagicMock(document=MagicMock(return_value=thread_ref)),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request") as fetch_graph_message, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        fetch_graph_message.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        failure_doc.reference.set.assert_called_once()
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertFalse(update_payload["retryable"])
        self.assertEqual("blocked_existing_outbound_artifact", update_payload["recoveryStatus"])
        self.assertIn("outbox", update_payload["lastRetryError"])
        self.assertEqual("outbox-existing", update_payload["recoveryArtifactId"])

    def test_retry_processing_failures_blocks_after_graph_identity_matches_outbox(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__internet-message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "retryable": True,
            "processingAttempts": 0,
        }
        outbox_doc = MagicMock()
        outbox_doc.id = "outbox-existing"
        outbox_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "status": "queued",
        }

        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        outbox_collection = MagicMock()
        outbox_collection.limit.return_value.stream.side_effect = [[], [outbox_doc]]
        empty_collection = MagicMock()
        empty_collection.limit.return_value.stream.return_value = []
        thread_ref = MagicMock()
        thread_ref.get.return_value.exists = False

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "outbox": outbox_collection,
            "pendingResponses": empty_collection,
            "deadLetterQueue": empty_collection,
            "actionAudit": empty_collection,
            "clients": MagicMock(document=MagicMock(return_value=MagicMock(collection=MagicMock(return_value=empty_collection)))),
            "threads": MagicMock(document=MagicMock(return_value=thread_ref)),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc
        graph_response = MagicMock()
        graph_response.json.return_value = {
            "id": "graph-message-1",
            "internetMessageId": "<internet-message-1@example.test>",
            "conversationId": "conversation-1",
        }

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request", return_value=graph_response) as fetch_graph_message, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        fetch_graph_message.assert_called_once()
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertEqual("blocked_existing_outbound_artifact", update_payload["recoveryStatus"])
        self.assertEqual("outbox-existing", update_payload["recoveryArtifactId"])

    def test_retry_processing_failures_blocks_when_conversation_was_manually_continued(self):
        created_at = datetime(2026, 6, 22, 2, 19, tzinfo=timezone.utc)
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__internet-message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "retryable": True,
            "processingAttempts": 0,
            "createdAt": created_at,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        empty_collection = MagicMock()
        empty_collection.limit.return_value.stream.return_value = []
        thread_ref = MagicMock()
        thread_ref.get.return_value.exists = False

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "outbox": empty_collection,
            "pendingResponses": empty_collection,
            "deadLetterQueue": empty_collection,
            "actionAudit": empty_collection,
            "clients": MagicMock(document=MagicMock(return_value=MagicMock(collection=MagicMock(return_value=empty_collection)))),
            "threads": MagicMock(document=MagicMock(return_value=thread_ref)),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc

        graph_response = MagicMock()
        graph_response.status_code = 200
        graph_response.json.return_value = {
            "id": "graph-message-1",
            "internetMessageId": "<internet-message-1@example.test>",
            "conversationId": "conversation-1",
        }
        sent_items_response = MagicMock()
        sent_items_response.status_code = 200
        sent_items_response.json.return_value = {
            "value": [
                {
                    "id": "sent-manual-1",
                    "internetMessageId": "<manual-reply@example.test>",
                    "conversationId": "conversation-1",
                    "subject": "RE: 16 Jupiter Ln",
                    "toRecipients": [{"emailAddress": {"address": "broker@example.test"}}],
                    "sentDateTime": "2026-06-22T03:00:00Z",
                }
            ]
        }
        requests_seen = []

        def fake_get(url, **kwargs):
            requests_seen.append((url, kwargs))
            if "/mailFolders/SentItems/messages" in url:
                return sent_items_response
            return graph_response

        def run_request(request_fn):
            return request_fn()

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request", side_effect=run_request), \
             patch.object(processing.requests, "get", side_effect=fake_get), \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        sent_query = next(kwargs for url, kwargs in requests_seen if "/mailFolders/SentItems/messages" in url)
        self.assertNotIn("body", sent_query["params"]["$select"])
        self.assertNotIn("bodyPreview", sent_query["params"]["$select"])
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertFalse(update_payload["retryable"])
        self.assertEqual("blocked_manual_conversation_continued", update_payload["recoveryStatus"])
        self.assertEqual("sent-manual-1", update_payload["recoverySentMessageId"])
        self.assertEqual("<manual-reply@example.test>", update_payload["recoverySentInternetMessageId"])

    def test_retry_processing_failures_blocks_existing_handled_event_for_source_message(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__internet-message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "retryable": True,
            "processingAttempts": 0,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        empty_collection = MagicMock()
        empty_collection.limit.return_value.stream.return_value = []
        thread_snapshot = MagicMock()
        thread_snapshot.exists = True
        thread_snapshot.to_dict.return_value = {
            "handledEvents": {
                "wrong_contact:broker@example.test": {
                    "detectedInMessageId": "graph-message-1",
                    "notificationId": "notification-1",
                }
            }
        }
        thread_ref = MagicMock()
        thread_ref.get.return_value = thread_snapshot

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "outbox": empty_collection,
            "pendingResponses": empty_collection,
            "deadLetterQueue": empty_collection,
            "actionAudit": empty_collection,
            "clients": MagicMock(document=MagicMock(return_value=MagicMock(collection=MagicMock(return_value=empty_collection)))),
            "threads": MagicMock(document=MagicMock(return_value=thread_ref)),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc
        graph_response = MagicMock()
        graph_response.json.return_value = {
            "id": "graph-message-1",
            "internetMessageId": "<internet-message-1@example.test>",
            "conversationId": "conversation-1",
        }

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request", return_value=graph_response), \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertEqual("blocked_existing_outbound_artifact", update_payload["recoveryStatus"])
        self.assertEqual("threads/thread-1/handledEvents", update_payload["recoveryArtifactCollection"])

    def test_retry_processing_failures_blocks_when_visibility_guard_cannot_scan_outbox(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 0,
        }

        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        unreadable_outbox = MagicMock()
        unreadable_outbox.limit.return_value.stream.side_effect = RuntimeError("firestore unavailable")
        empty_collection = MagicMock()
        empty_collection.limit.return_value.stream.return_value = []

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "outbox": unreadable_outbox,
            "pendingResponses": empty_collection,
            "deadLetterQueue": empty_collection,
            "actionAudit": empty_collection,
            "clients": MagicMock(document=MagicMock(return_value=MagicMock(collection=MagicMock(return_value=empty_collection)))),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "exponential_backoff_request") as fetch_graph_message, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        fetch_graph_message.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        failure_doc.reference.set.assert_called_once()
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertFalse(update_payload["retryable"])
        self.assertEqual("blocked_retry_guard_unreadable", update_payload["recoveryStatus"])
        self.assertIn("Could not verify duplicate-send guard", update_payload["lastRetryError"])

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

    def test_new_property_duplicate_check_normalizes_non_string_sheet_cells(self):
        class SheetWithNumericCells:
            def spreadsheets(self):
                return self

            def values(self):
                return self

            def get(self, **_kwargs):
                return self

            def execute(self):
                return {
                    "values": [
                        [777, None],
                        ["888 Replacement Signal Ave", 123],
                    ]
                }

        header = ["Property Address", "City"]

        self.assertTrue(
            processing._property_exists_in_sheet(
                SheetWithNumericCells(),
                "sheet-1",
                "Properties",
                header,
                "777",
                "",
            )
        )
        self.assertTrue(
            processing._property_exists_in_sheet(
                SheetWithNumericCells(),
                "sheet-1",
                "Properties",
                header,
                "888 Replacement Signal Ave",
                "123",
            )
        )


if __name__ == "__main__":
    unittest.main()
