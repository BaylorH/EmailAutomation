import unittest
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError
from httplib2 import Response

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import ai_processing, processing
from email_automation.campaign_safety import CampaignAutomationDecision


class _ThreadLookupSnapshot:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class _ThreadLookupNode:
    def __init__(self, root, path=()):
        self._root = root
        self._path = path

    def collection(self, name):
        return _ThreadLookupNode(self._root, self._path + (name,))

    def document(self, doc_id):
        return _ThreadLookupNode(self._root, self._path + (doc_id,))

    def get(self):
        self._root.get_calls.append(self._path)
        return _ThreadLookupSnapshot(self._root.documents.get(self._path))


class _ThreadLookupFirestore:
    def __init__(self, user_id, thread_id, client_id):
        self.documents = {
            ("users", user_id, "threads", thread_id): {"clientId": client_id},
        }
        self.get_calls = []

    def collection(self, name):
        return _ThreadLookupNode(self, (name,))


class ProcessingRetryabilityTests(unittest.TestCase):
    def setUp(self):
        self._campaign_decision_patch = patch.object(
            processing,
            "get_client_automation_decision",
            return_value=CampaignAutomationDecision(
                state="allow",
                reason="",
                client_data={"status": "live"},
                metadata={"terminal": False, "stopKind": "none"},
            ),
            create=True,
        )
        self.campaign_decision = self._campaign_decision_patch.start()
        self.addCleanup(self._campaign_decision_patch.stop)

    def test_retryable_ai_failures_do_not_mark_messages_processed(self):
        self.assertFalse(
            processing._should_mark_processed_after_error(
                processing.RetryableProcessingError("AI proposal unavailable")
            )
        )
        self.assertFalse(processing._should_mark_processed_after_error(ValueError("unexpected bug")))
        self.assertTrue(processing._should_mark_processed_after_error(None))

    def test_sheet_apply_429_escapes_for_retryable_failure_recording(self):
        quota_error = HttpError(
            Response({"status": "429"}),
            b'{"error":{"message":"read requests per minute exceeded"}}',
        )
        sheets = MagicMock()

        with patch.object(ai_processing, "_sheets_client", return_value=sheets), \
             patch.object(ai_processing, "_get_first_tab_title", return_value="Properties"), \
             patch.object(ai_processing, "_ensure_ai_meta_tab"), \
             patch.object(ai_processing, "_load_ai_meta_rows", return_value=[]), \
             patch.object(ai_processing, "_execute_with_retry", side_effect=quota_error):
            with self.assertRaises(HttpError) as raised:
                ai_processing.apply_proposal_to_sheet(
                    "uid-1",
                    "client-1",
                    "sheet-1",
                    ["Property Address", "Total SF"],
                    3,
                    ["4402 Rex Rd", ""],
                    {
                        "updates": [{
                            "column": "Total SF",
                            "value": "10000",
                            "confidence": 0.99,
                            "reason": "Broker stated the total.",
                        }]
                    },
                )

        self.assertEqual(429, raised.exception.resp.status)

    def test_terminal_campaign_suppression_does_not_queue_auto_reply_retry(self):
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            outcome="blocked_campaign_terminal",
            error="client_stopped_by_user",
            sent_but_unindexed=False,
            campaign_suppression_kind="terminal",
        )

        with patch.object(processing, "queue_pending_response") as queue_pending, \
             patch.object(processing, "record_sent_unindexed_response") as reconcile:
            outcome = processing._queue_response_retry_or_reconciliation(
                "uid-1",
                "thread-1",
                "message-1",
                "bp21harrison@gmail.com",
                "Thanks for the update.",
                "client-1",
            )

        self.assertEqual("campaign_stopped", outcome)
        queue_pending.assert_not_called()
        reconcile.assert_not_called()

    def test_scan_records_unexpected_processing_crash_without_marking_processed(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
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

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "exponential_backoff_request", return_value=response), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_match_message_to_thread", return_value="thread-1"), \
             patch.object(processing, "_has_processing_failure_record", return_value=False), \
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
            "client-1",
            "thread-1",
            "<message-1@example.test>",
            "flyer_links crash",
        )
        mark_processed.assert_not_called()
        self.assertEqual(0, result["processed"])
        self.assertEqual(
            [("users", "uid-1", "threads", "thread-1")],
            fake_fs.get_calls,
        )

    def test_scan_skips_inbox_retry_when_user_manually_continued_conversation(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
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

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "exponential_backoff_request", return_value=response), \
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
            "client-1",
            "thread-1",
            "<message-1@example.test>",
            manual_continuation,
        )
        mark_processed.assert_called_once_with("uid-1", "<message-1@example.test>")
        self.assertEqual(0, result["processed"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual(
            [("users", "uid-1", "threads", "thread-1")],
            fake_fs.get_calls,
        )

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
        warning_fallback_doc = MagicMock()
        warning_fallback_doc.id = "thread-4__message-warning__asset_warning_persistence"
        warning_fallback_doc.to_dict.return_value = {
            "threadId": "thread-4",
            "messageId": "message-warning",
            "retryable": False,
            "recoveryStatus": "asset_warning_persistence_failed",
        }
        operator_replay_doc = MagicMock()
        operator_replay_doc.id = "thread-5__message-replay"
        operator_replay_doc.to_dict.return_value = {
            "threadId": "thread-5",
            "messageId": "message-replay",
            "retryable": True,
            "recoveryStatus": "operator_replay_in_progress",
        }

        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [
            processed_doc,
            retry_doc,
            missing_id_doc,
            warning_fallback_doc,
            operator_replay_doc,
        ]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection

        def fake_has_processed(_user_id, message_id) -> bool:
            return message_id in {
                "message-processed",
                "message-warning",
                "message-replay",
            }

        with patch.object(processing, "_fs", fake_fs), patch.object(processing, "has_processed", side_effect=fake_has_processed):
            result = processing.reconcile_stale_processing_failures("uid-1")

        self.assertEqual({"checked": 5, "cleared": 1, "retained": 4}, result)
        processed_doc.reference.delete.assert_called_once()
        retry_doc.reference.delete.assert_not_called()
        missing_id_doc.reference.delete.assert_not_called()
        warning_fallback_doc.reference.delete.assert_not_called()
        operator_replay_doc.reference.delete.assert_not_called()

    def test_retry_processing_failures_preserves_operator_replay_claim(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 0,
            "recoveryStatus": "operator_replay_in_progress",
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=True) as has_processed, \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_message, \
             patch.object(processing, "process_inbox_message") as process_message:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        has_processed.assert_not_called()
        fetch_message.assert_not_called()
        process_message.assert_not_called()
        failure_doc.reference.delete.assert_not_called()

    def test_retry_processing_failures_processes_exact_graph_message_and_clears_success(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 1,
            # Production always writes a timezone-aware creation timestamp; the
            # sent-mail continuation guard now fails CLOSED on an absent/unusable
            # sent_after, so the fixture must carry a realistic one.
            "createdAt": datetime.now(timezone.utc) - timedelta(minutes=30),
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
             patch.object(processing, "find_sent_conversation_continuation_for_retry", return_value=None), \
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

    def test_retry_processing_failures_preserves_work_when_campaign_is_maintenance_paused(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 1,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={"status": "live", "automationPaused": True},
            metadata={"terminal": False, "stopKind": "maintenance_pause"},
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_message, \
             patch.object(processing, "process_inbox_message") as process_message:
            result = processing.retry_processing_failures(
                "uid-1", {"Authorization": "Bearer fake"}
            )

        self.assertEqual(1, result["skipped"])
        self.assertEqual(0, result["retried"])
        fetch_message.assert_not_called()
        process_message.assert_not_called()
        failure_doc.reference.delete.assert_not_called()
        payload = failure_doc.reference.set.call_args.args[0]
        self.assertTrue(payload["retryable"])
        self.assertEqual(1, payload["processingAttempts"])
        self.assertEqual("blocked", payload["automationSuppressedState"])

    def test_maintenance_never_resurrects_non_retryable_processing_failure(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": False,
            "processingAttempts": 1,
            "recoveryStatus": "blocked_manual_continuation",
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={"status": "live", "automationPaused": True},
            metadata={"terminal": False, "stopKind": "maintenance_pause"},
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_message:
            result = processing.retry_processing_failures(
                "uid-1", {"Authorization": "Bearer fake"}
            )

        self.assertEqual(1, result["skipped"])
        fetch_message.assert_not_called()
        payload = failure_doc.reference.set.call_args.args[0]
        self.assertFalse(payload["retryable"])

    def test_asset_warning_fallback_is_not_rewritten_by_campaign_suppression(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1__asset_warning_persistence"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": False,
            "processingAttempts": 0,
            "recoveryStatus": "asset_warning_persistence_failed",
            "metadata": {"assetWarnings": [{"name": "dead.pdf", "error": "404"}]},
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="blocked",
            reason="client_stopped_by_user",
            client_data={"status": "stopped"},
            metadata={"terminal": True, "stopKind": "user_stop"},
        )

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing, "_fetch_graph_message_by_id"
        ) as fetch_message:
            result = processing.retry_processing_failures(
                "uid-1", {"Authorization": "Bearer fake"}
            )

        self.assertEqual(1, result["skipped"])
        fetch_message.assert_not_called()
        failure_doc.reference.set.assert_not_called()
        failure_doc.reference.delete.assert_not_called()

    def test_retry_processing_failures_preserves_work_when_campaign_state_is_unknown(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "message-1",
            "retryable": True,
            "processingAttempts": 2,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = failures_collection
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="unknown",
            reason="client_automation_state_read_error",
            client_data={},
            metadata={"terminal": False, "stopKind": "none"},
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_message:
            result = processing.retry_processing_failures(
                "uid-1", {"Authorization": "Bearer fake"}
            )

        self.assertEqual(1, result["skipped"])
        fetch_message.assert_not_called()
        payload = failure_doc.reference.set.call_args.args[0]
        self.assertTrue(payload["retryable"])
        self.assertEqual(2, payload["processingAttempts"])
        self.assertEqual("unknown", payload["automationSuppressedState"])

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
