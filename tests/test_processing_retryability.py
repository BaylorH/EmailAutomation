import unittest
import os
import hashlib
from copy import deepcopy
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
        if self._root.reject_unsafe_document_ids and (
            "/" in doc_id or len(doc_id.encode("utf-8")) > 1_500
        ):
            raise ValueError("invalid Firestore document ID")
        return _ThreadLookupNode(self._root, self._path + (doc_id,))

    def get(self):
        self._root.get_calls.append(self._path)
        return _ThreadLookupSnapshot(self._root.documents.get(self._path))

    def set(self, payload, merge=False):
        self._root.set_calls.append((self._path, deepcopy(payload), merge))
        if merge:
            current = dict(self._root.documents.get(self._path) or {})
            current.update(deepcopy(payload))
            self._root.documents[self._path] = current
        else:
            self._root.documents[self._path] = deepcopy(payload)


class _ThreadLookupFirestore:
    def __init__(self, user_id, thread_id, client_id):
        self.documents = {
            ("users", user_id, "threads", thread_id): {"clientId": client_id},
        }
        self.get_calls = []
        self.set_calls = []
        self.reject_unsafe_document_ids = False

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

    def test_reply_review_recovery_uses_one_safe_domain_separated_record_id(self):
        user_id = "uid-1"
        client_id = "client-1"
        thread_id = "thread-" + ("t" * (1_500 - len("thread-")))
        canonical_key = "<slash/" + (
            "r" * (1_500 - len("<slash/>"))
        ) + ">"
        graph_id = "graph-message-1"
        self.assertEqual(1_500, len(thread_id))
        self.assertEqual(1_500, len(canonical_key))
        self.assertNotEqual(graph_id, canonical_key)

        error = processing.ReplyReviewProjectionPendingError(
            projection_intent={
                "clientId": client_id,
                "threadId": thread_id,
                "sourceGraphMessageId": graph_id,
                "recipient": "contact@example.test",
                "responseBody": "Hi,\n\nThanks.",
                "subject": None,
                "conversationId": "conversation-1",
                "terminalDisposition": None,
            }
        )
        message = {
            "id": graph_id,
            "internetMessageId": canonical_key,
        }
        fake_fs = _ThreadLookupFirestore(user_id, thread_id, client_id)
        fake_fs.reject_unsafe_document_ids = True

        with patch.object(processing, "_fs", fake_fs):
            recorded = processing._record_inbox_processing_failure(
                user_id,
                client_id,
                thread_id,
                canonical_key,
                error,
                message,
            )
            guarded = processing._has_pending_reply_review_projection_recovery(
                user_id,
                thread_id,
                canonical_key,
                message,
            )

        self.assertTrue(recorded)
        self.assertTrue(guarded)
        self.assertEqual(1, len(fake_fs.set_calls))
        written_path = fake_fs.set_calls[0][0]
        read_path = fake_fs.get_calls[-1]
        self.assertEqual(written_path, read_path)
        record_id = written_path[-1]
        self.assertTrue(record_id.startswith("reply_review_projection__"))
        self.assertEqual(len("reply_review_projection__") + 64, len(record_id))
        self.assertNotIn("/", record_id)
        self.assertNotIn("thread", record_id)
        self.assertNotIn("slash", record_id)

        domain = b"emailautomation:reply-review-projection-recovery:v1\x00"
        thread_bytes = thread_id.encode("utf-8")
        key_bytes = canonical_key.encode("utf-8")
        expected_digest = hashlib.sha256(
            domain
            + len(thread_bytes).to_bytes(4, "big")
            + thread_bytes
            + len(key_bytes).to_bytes(4, "big")
            + key_bytes
        ).hexdigest()
        self.assertEqual(
            f"reply_review_projection__{expected_digest}",
            record_id,
        )
        self.assertNotEqual(
            "reply_review_projection__"
            + hashlib.sha256(f"{thread_id}__{canonical_key}".encode()).hexdigest(),
            record_id,
        )
        stored = fake_fs.documents[written_path]
        self.assertEqual(thread_id, stored["threadId"])
        self.assertEqual(canonical_key, stored["messageId"])
        self.assertEqual(graph_id, stored["metadata"]["sourceGraphMessageId"])
        self.assertEqual(
            canonical_key, stored["metadata"]["sourceInternetMessageId"]
        )

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
             patch.object(processing, "_resolve_current_mailbox_email", return_value="operator@example.test"), \
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
            [
                (
                    "users",
                    "uid-1",
                    "processingFailures",
                    processing._reply_review_projection_failure_record_id(
                        "thread-1", "<message-1@example.test>"
                    ),
                ),
                ("users", "uid-1", "threads", "thread-1"),
            ],
            fake_fs.get_calls,
        )

    def test_scan_records_one_canonical_reply_review_projection_failure(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        received_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        message = {
            "id": "graph-message-1",
            "internetMessageId": "<internet-message-1@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": received_now,
        }
        response.json.return_value = {"value": [message]}
        projection_error = processing.ReplyReviewProjectionPendingError(
            projection_intent={
                "clientId": "client-1",
                "threadId": "thread-1",
                "sourceGraphMessageId": "graph-message-1",
                "recipient": "contact@example.test",
                "responseBody": "Hi,\n\nThanks.",
                "subject": "RE: 4402 Rex Rd",
                "conversationId": "conversation-1",
                "terminalDisposition": None,
            }
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "exponential_backoff_request", return_value=response), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_match_message_to_thread", return_value="thread-1"), \
             patch.object(processing, "_resolve_current_mailbox_email", return_value="operator@example.test"), \
             patch.object(processing, "_has_processing_failure_record", return_value=False), \
             patch.object(processing, "process_inbox_message", side_effect=projection_error), \
             patch.object(processing, "_record_reply_review_projection_failure", return_value=True) as record_failure, \
             patch.object(processing, "mark_processed") as mark_processed, \
             patch.object(processing, "set_last_scan_iso"):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        record_failure.assert_called_once()
        self.assertEqual(
            ("uid-1", "client-1", "thread-1", "<internet-message-1@example.test>"),
            record_failure.call_args.args[:4],
        )
        metadata = record_failure.call_args.kwargs["metadata"]
        self.assertEqual("graph-message-1", metadata["sourceGraphMessageId"])
        self.assertEqual(
            "<internet-message-1@example.test>",
            metadata["sourceInternetMessageId"],
        )
        self.assertEqual(
            "<internet-message-1@example.test>",
            metadata["canonicalProcessedKey"],
        )
        mark_processed.assert_not_called()
        self.assertEqual(0, result["processed"])

    def test_scan_defers_existing_reply_review_projection_recovery_without_replay(self):
        user_id = "uid-1"
        thread_id = "thread-1"
        client_id = "client-1"
        graph_id = "graph-message-1"
        internet_id = "<internet-message-1@example.test>"
        failure_id = processing._reply_review_projection_failure_record_id(
            thread_id, internet_id
        )
        fake_fs = _ThreadLookupFirestore(user_id, thread_id, client_id)
        fake_fs.documents[("users", user_id, "processingFailures", failure_id)] = {
            "clientId": client_id,
            "threadId": thread_id,
            "messageId": internet_id,
            "retryable": True,
            "processingAttempts": 0,
            "recoveryStatus": "reply_review_projection_pending",
            "metadata": {
                "kind": "policy_blocked_reply_review",
                "schemaVersion": 1,
                "clientId": client_id,
                "threadId": thread_id,
                "canonicalProcessedKey": internet_id,
                "sourceGraphMessageId": graph_id,
                "sourceInternetMessageId": internet_id,
                "recipient": "contact@example.test",
                "responseBody": "Hi,\n\nThanks.",
                "subject": "RE: 4402 Rex Rd",
                "conversationId": "conversation-1",
                "terminalDisposition": None,
            },
        }
        response = MagicMock()
        response.json.return_value = {
            "value": [{
                "id": graph_id,
                "internetMessageId": internet_id,
                "subject": "RE: 4402 Rex Rd",
                "receivedDateTime": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            }]
        }

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "exponential_backoff_request", return_value=response), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_match_message_to_thread", return_value=thread_id), \
             patch.object(processing, "_resolve_current_mailbox_email", return_value="operator@example.test"), \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "send_reply_in_thread") as send_reply, \
             patch.object(processing, "apply_proposal_to_sheet") as apply_sheet, \
             patch.object(processing, "mark_event_handled") as mark_event, \
             patch.object(processing, "mark_processed") as mark_processed, \
             patch.object(processing, "set_last_scan_iso"):
            result = processing.scan_inbox_against_index(
                user_id,
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        self.assertEqual(0, result["processed"])
        self.assertEqual(1, result["skipped"])
        process_message.assert_not_called()
        send_reply.assert_not_called()
        apply_sheet.assert_not_called()
        mark_event.assert_not_called()
        mark_processed.assert_not_called()

    def test_pending_reply_review_scan_guard_fails_closed_for_tamper_or_read_error(self):
        user_id = "uid-1"
        thread_id = "thread-1"
        internet_id = "<internet-message-1@example.test>"
        failure_id = processing._reply_review_projection_failure_record_id(
            thread_id, internet_id
        )
        message = {
            "id": "graph-message-1",
            "internetMessageId": internet_id,
        }
        tampered_fs = _ThreadLookupFirestore(user_id, thread_id, "client-1")
        tampered_fs.documents[
            ("users", user_id, "processingFailures", failure_id)
        ] = {
            "clientId": "client-1",
            "threadId": thread_id,
            "messageId": internet_id,
            "recoveryStatus": "reply_review_projection_pending",
            "metadata": {"kind": "policy_blocked_reply_review", "unexpected": True},
        }

        with patch.object(processing, "_fs", tampered_fs):
            self.assertTrue(processing._has_pending_reply_review_projection_recovery(
                user_id,
                thread_id,
                internet_id,
                message,
            ))

        unreadable_fs = MagicMock()
        unreadable_fs.collection.return_value.document.return_value.collection.return_value.document.return_value.get.side_effect = RuntimeError(
            "firestore unavailable"
        )
        with patch.object(processing, "_fs", unreadable_fs):
            self.assertTrue(processing._has_pending_reply_review_projection_recovery(
                user_id,
                thread_id,
                internet_id,
                message,
            ))

        generic_fs = _ThreadLookupFirestore(user_id, thread_id, "client-1")
        generic_fs.documents[
            ("users", user_id, "processingFailures", failure_id)
        ] = {
            "clientId": "client-1",
            "threadId": thread_id,
            "messageId": internet_id,
            "recoveryStatus": "retryable_processing_failure",
            "reason": "proposal unavailable",
        }
        with patch.object(processing, "_fs", generic_fs):
            self.assertFalse(processing._has_pending_reply_review_projection_recovery(
                user_id,
                thread_id,
                internet_id,
                message,
            ))

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
             patch.object(processing, "_resolve_current_mailbox_email", return_value="operator@example.test"), \
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
            [
                (
                    "users",
                    "uid-1",
                    "processingFailures",
                    processing._reply_review_projection_failure_record_id(
                        "thread-1", "<message-1@example.test>"
                    ),
                ),
                ("users", "uid-1", "threads", "thread-1"),
            ],
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

    def test_temporary_campaign_suppression_preserves_pending_projection_then_recovers(self):
        stored = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "retryable": True,
            "processingAttempts": 0,
            "recoveryStatus": "reply_review_projection_pending",
            "metadata": {
                "kind": "policy_blocked_reply_review",
                "schemaVersion": 1,
                "clientId": "client-1",
                "threadId": "thread-1",
                "canonicalProcessedKey": "<internet-message-1@example.test>",
                "sourceGraphMessageId": "graph-message-1",
                "sourceInternetMessageId": "<internet-message-1@example.test>",
                "recipient": "contact@example.test",
                "responseBody": "Hi,\n\nThanks.",
                "subject": "RE: 16 Jupiter Ln",
                "conversationId": "conversation-1",
                "terminalDisposition": None,
            },
        }
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__<internet-message-1@example.test>"
        failure_doc.to_dict.side_effect = lambda: deepcopy(stored)
        failure_doc.reference.set.side_effect = (
            lambda payload, merge=True: stored.update(deepcopy(payload))
        )
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )
        self.campaign_decision.side_effect = [
            CampaignAutomationDecision(
                state="blocked",
                reason="campaign_maintenance",
                client_data={"status": "live", "automationPaused": True},
                metadata={"terminal": False, "stopKind": "maintenance_pause"},
            ),
            CampaignAutomationDecision(
                state="allow",
                reason="",
                client_data={"status": "live"},
                metadata={"terminal": False, "stopKind": "none"},
            ),
        ]

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "create_policy_blocked_reply_review", return_value=MagicMock()) as create_review, \
             patch.object(processing, "mark_processed", return_value=True) as mark_processed, \
             patch.object(processing, "has_processed") as has_processed, \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_graph, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "send_reply_in_thread") as send_reply:
            suppressed = processing.retry_processing_failures(
                "uid-1", {"Authorization": "Bearer fake"}
            )
            recovered = processing.retry_processing_failures(
                "uid-1", {"Authorization": "Bearer fake"}
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            suppressed,
        )
        self.assertEqual(
            {"checked": 1, "retried": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            recovered,
        )
        self.assertEqual("reply_review_projection_pending", stored["recoveryStatus"])
        self.assertTrue(stored["retryable"])
        self.assertEqual("blocked", stored["automationSuppressedState"])
        self.assertEqual("campaign_maintenance", stored["automationSuppressedReason"])
        create_review.assert_called_once()
        self.assertEqual(
            [
                unittest.mock.call("uid-1", "<internet-message-1@example.test>"),
                unittest.mock.call("uid-1", "graph-message-1"),
            ],
            mark_processed.call_args_list,
        )
        failure_doc.reference.delete.assert_called_once()
        has_processed.assert_not_called()
        fetch_graph.assert_not_called()
        process_message.assert_not_called()
        send_reply.assert_not_called()

    def test_terminal_campaign_suppression_preserves_reply_review_recovery_status(self):
        for recovery_status, original_retryable in (
            ("reply_review_projection_pending", True),
            ("reply_review_projection_manual_review", False),
        ):
            with self.subTest(recovery_status=recovery_status):
                stored = {
                    "clientId": "client-1",
                    "threadId": "thread-1",
                    "messageId": "<internet-message-1@example.test>",
                    "retryable": original_retryable,
                    "processingAttempts": 1,
                    "recoveryStatus": recovery_status,
                }
                failure_doc = MagicMock()
                failure_doc.to_dict.side_effect = lambda: deepcopy(stored)
                failure_doc.reference.set.side_effect = (
                    lambda payload, merge=True: stored.update(deepcopy(payload))
                )
                failures_collection = MagicMock()
                failures_collection.limit.return_value.stream.return_value = [failure_doc]
                fake_fs = MagicMock()
                fake_fs.collection.return_value.document.return_value.collection.return_value = (
                    failures_collection
                )
                self.campaign_decision.return_value = CampaignAutomationDecision(
                    state="blocked",
                    reason="client_stopped_by_user",
                    client_data={"status": "stopped"},
                    metadata={"terminal": True, "stopKind": "user_stop"},
                )

                with patch.object(processing, "_fs", fake_fs), \
                     patch.object(processing, "_fetch_graph_message_by_id") as fetch_graph, \
                     patch.object(processing, "process_inbox_message") as process_message, \
                     patch.object(processing, "create_policy_blocked_reply_review") as create_review:
                    result = processing.retry_processing_failures(
                        "uid-1", {"Authorization": "Bearer fake"}
                    )

                self.assertEqual(1, result["skipped"])
                update = failure_doc.reference.set.call_args.args[0]
                self.assertNotIn("recoveryStatus", update)
                self.assertEqual(recovery_status, stored["recoveryStatus"])
                self.assertFalse(stored["retryable"])
                self.assertEqual("blocked", stored["automationSuppressedState"])
                self.assertEqual(
                    "client_stopped_by_user", stored["automationSuppressedReason"]
                )
                fetch_graph.assert_not_called()
                process_message.assert_not_called()
                create_review.assert_not_called()

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
            # Also answers the internet-id -> provider-id translation the retry
            # now performs first. The parked record stores an internet
            # Message-ID, and asking the provider for a message BY that id was
            # rejected every time, which is what made the retry queue undrainable.
            "value": [{"id": "graph-message-1"}],
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
        # Two provider round trips now, not one, and that is the fix rather than
        # a regression: the parked record holds an INTERNET Message-ID, so the
        # retry must translate it into the provider's own id before it can ask
        # for the message. Asking with the wrong kind of id is what made every
        # retry fail with 400 and the queue undrainable.
        self.assertEqual(2, fetch_graph_message.call_count)
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
            # Also answers the internet-id -> provider-id translation the retry
            # now performs first. The parked record stores an internet
            # Message-ID, and asking the provider for a message BY that id was
            # rejected every time, which is what made the retry queue undrainable.
            "value": [{"id": "graph-message-1"}],
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
            # Also answers the internet-id -> provider-id translation the retry
            # now performs first. The parked record stores an internet
            # Message-ID, and asking the provider for a message BY that id was
            # rejected every time, which is what made the retry queue undrainable.
            "value": [{"id": "graph-message-1"}],
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

    def test_retry_processing_failures_recovers_only_stored_reply_review_projection(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__graph-message-1"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "retryable": True,
            "processingAttempts": 0,
            "recoveryStatus": "reply_review_projection_pending",
            "metadata": {
                "kind": "policy_blocked_reply_review",
                "schemaVersion": 1,
                "clientId": "client-1",
                "threadId": "thread-1",
                "canonicalProcessedKey": "<internet-message-1@example.test>",
                "sourceGraphMessageId": "graph-message-1",
                "sourceInternetMessageId": "<internet-message-1@example.test>",
                "recipient": "contact@example.test",
                "responseBody": "Hi,\n\nThanks.",
                "subject": "RE: 16 Jupiter Ln",
                "conversationId": "conversation-1",
                "terminalDisposition": None,
            },
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        empty_collection = MagicMock()
        empty_collection.limit.return_value.stream.return_value = []
        thread_snapshot = MagicMock()
        thread_snapshot.exists = True
        thread_snapshot.to_dict.return_value = {
            "handledEvents": {
                "property_unavailable:no_longer_available": {
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
            "clients": MagicMock(
                document=MagicMock(
                    return_value=MagicMock(
                        collection=MagicMock(return_value=empty_collection)
                    )
                )
            ),
            "threads": MagicMock(document=MagicMock(return_value=thread_ref)),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc
        projection = MagicMock(
            review_id="review-1",
            notification_id="notification-2",
            status="created",
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "create_policy_blocked_reply_review", return_value=projection) as create_review, \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_graph_message, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "queue_pending_response") as queue_pending, \
             patch.object(processing, "record_sent_unindexed_response") as reconcile, \
             patch.object(processing, "send_reply_in_thread") as send_reply, \
             patch.object(processing, "mark_processed", return_value=True) as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            result,
        )
        create_review.assert_called_once_with(
            user_id="uid-1",
            client_id="client-1",
            thread_id="thread-1",
            source_message_id="graph-message-1",
            recipient="contact@example.test",
            response_body="Hi,\n\nThanks.",
            subject="RE: 16 Jupiter Ln",
            conversation_id="conversation-1",
            terminal_disposition=None,
        )
        fetch_graph_message.assert_not_called()
        process_message.assert_not_called()
        queue_pending.assert_not_called()
        reconcile.assert_not_called()
        send_reply.assert_not_called()
        self.assertEqual(
            [
                unittest.mock.call("uid-1", "<internet-message-1@example.test>"),
                unittest.mock.call("uid-1", "graph-message-1"),
            ],
            mark_processed.call_args_list,
        )
        failure_doc.reference.delete.assert_called_once()

    def test_stored_reply_review_projection_rejects_noncanonical_or_invalid_intent(self):
        base = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "recoveryStatus": "reply_review_projection_pending",
            "metadata": {
                "kind": "policy_blocked_reply_review",
                "schemaVersion": 1,
                "clientId": "client-1",
                "threadId": "thread-1",
                "canonicalProcessedKey": "<internet-message-1@example.test>",
                "sourceGraphMessageId": "graph-message-1",
                "sourceInternetMessageId": "<internet-message-1@example.test>",
                "recipient": "contact@example.test",
                "responseBody": "Hi,\n\nThanks.",
                "subject": "RE: 16 Jupiter Ln",
                "conversationId": "conversation-1",
                "terminalDisposition": None,
            },
        }

        invalid_records = []
        extra = deepcopy(base)
        extra["metadata"]["unexpected"] = "value"
        invalid_records.append(("extra field", extra))
        missing = deepcopy(base)
        del missing["metadata"]["responseBody"]
        invalid_records.append(("missing field", missing))
        bool_version = deepcopy(base)
        bool_version["metadata"]["schemaVersion"] = True
        invalid_records.append(("bool schema version", bool_version))
        mismatched_client = deepcopy(base)
        mismatched_client["metadata"]["clientId"] = "client-2"
        invalid_records.append(("client identity mismatch", mismatched_client))
        mismatched_thread = deepcopy(base)
        mismatched_thread["metadata"]["threadId"] = "thread-2"
        invalid_records.append(("thread identity mismatch", mismatched_thread))
        mismatched_canonical = deepcopy(base)
        mismatched_canonical["metadata"]["canonicalProcessedKey"] = "graph-message-1"
        invalid_records.append(("canonical identity mismatch", mismatched_canonical))
        invalid_recipient = deepcopy(base)
        invalid_recipient["metadata"]["recipient"] = "x" * 321
        invalid_records.append(("recipient bound", invalid_recipient))
        invalid_body = deepcopy(base)
        invalid_body["metadata"]["responseBody"] = "x" * 100_001
        invalid_records.append(("body bound", invalid_body))
        invalid_terminal = deepcopy(base)
        invalid_terminal["metadata"]["terminalDisposition"] = {
            "status": "completed",
            "reason": "all_fields_gathered",
            "rowNumber": True,
        }
        invalid_records.append(("bool terminal row", invalid_terminal))

        for label, record in invalid_records:
            with self.subTest(label=label), self.assertRaises(
                processing.ReplyReviewProjectionError
            ):
                processing._stored_reply_review_projection_intent("uid-1", record)

    def test_reply_review_projection_recovery_failure_is_bounded_and_local(self):
        failure_doc = MagicMock()
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "graph-message-1",
            "retryable": True,
            "processingAttempts": 0,
            "recoveryStatus": "reply_review_projection_pending",
            "metadata": {},
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(
                 processing,
                 "_retry_stored_reply_review_projection",
                 side_effect=processing.ReplyReviewProjectionError("transient write"),
             ) as retry_projection, \
             patch.object(processing, "has_processed") as has_processed, \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_graph, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "queue_pending_response") as queue_pending, \
             patch.object(processing, "record_sent_unindexed_response") as reconcile, \
             patch.object(processing, "send_reply_in_thread") as send_reply, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
                max_attempts=3,
            )

        self.assertEqual(
            {"checked": 1, "retried": 1, "succeeded": 0, "failed": 1, "skipped": 0},
            result,
        )
        retry_projection.assert_called_once()
        update = failure_doc.reference.set.call_args.args[0]
        self.assertEqual(1, update["processingAttempts"])
        self.assertTrue(update["retryable"])
        self.assertEqual(
            "policy-blocked reply review projection recovery failed",
            update["lastRetryError"],
        )
        self.assertNotIn("contact@example.test", update["lastRetryError"])
        self.assertNotIn("Hi", update["lastRetryError"])
        has_processed.assert_not_called()
        fetch_graph.assert_not_called()
        process_message.assert_not_called()
        queue_pending.assert_not_called()
        reconcile.assert_not_called()
        send_reply.assert_not_called()
        mark_processed.assert_not_called()
        failure_doc.reference.delete.assert_not_called()

    def test_reply_review_projection_recovery_retains_failure_after_partial_marker_write(self):
        failure_doc = MagicMock()
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "retryable": True,
            "processingAttempts": 0,
            "recoveryStatus": "reply_review_projection_pending",
            "metadata": {},
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )
        intent = {
            "client_id": "client-1",
            "thread_id": "thread-1",
            "source_message_id": "graph-message-1",
            "source_internet_message_id": "<internet-message-1@example.test>",
            "canonical_processed_key": "<internet-message-1@example.test>",
            "recipient": "contact@example.test",
            "response_body": "Hi,\n\nThanks.",
            "subject": None,
            "conversation_id": None,
            "terminal_disposition": None,
        }

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "_stored_reply_review_projection_intent", return_value=intent), \
             patch.object(processing, "create_policy_blocked_reply_review", return_value=MagicMock()), \
             patch.object(processing, "mark_processed", side_effect=[True, False]) as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
                max_attempts=3,
            )

        self.assertEqual(
            {"checked": 1, "retried": 1, "succeeded": 0, "failed": 1, "skipped": 0},
            result,
        )
        self.assertEqual(2, mark_processed.call_count)
        failure_doc.reference.delete.assert_not_called()
        update = failure_doc.reference.set.call_args.args[0]
        self.assertTrue(update["retryable"])
        self.assertEqual(1, update["processingAttempts"])

    def test_reconciliation_retains_pending_reply_review_after_partial_mark(self):
        failure_doc = MagicMock()
        failure_doc.to_dict.return_value = {
            "messageId": "<internet-message-1@example.test>",
            "recoveryStatus": "reply_review_projection_pending",
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing, "has_processed", return_value=True
        ) as has_processed:
            result = processing.reconcile_stale_processing_failures("uid-1")

        self.assertEqual({"checked": 1, "cleared": 0, "retained": 1}, result)
        has_processed.assert_not_called()
        failure_doc.reference.delete.assert_not_called()

    def test_identity_only_reply_review_failure_is_inert_and_retained(self):
        failure_doc = MagicMock()
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "<internet-message-1@example.test>",
            "retryable": False,
            "processingAttempts": 0,
            "recoveryStatus": "reply_review_projection_manual_review",
            "metadata": {
                "kind": "policy_blocked_reply_review",
                "schemaVersion": 1,
                "envelopeType": "identity_only",
                "failureCode": "invalid_projection_intent",
                "clientId": "client-1",
                "threadId": "thread-1",
                "canonicalProcessedKey": "<internet-message-1@example.test>",
                "sourceGraphMessageId": "graph-message-1",
                "sourceInternetMessageId": "<internet-message-1@example.test>",
            },
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "has_processed", return_value=True) as has_processed, \
             patch.object(processing, "_fetch_graph_message_by_id") as fetch_graph, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "send_reply_in_thread") as send_reply, \
             patch.object(processing, "queue_pending_response") as queue_pending, \
             patch.object(processing, "mark_processed") as mark_processed:
            retry_result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )
            reconcile_result = processing.reconcile_stale_processing_failures(
                "uid-1"
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            retry_result,
        )
        self.assertEqual(
            {"checked": 1, "cleared": 0, "retained": 1},
            reconcile_result,
        )
        fetch_graph.assert_not_called()
        process_message.assert_not_called()
        send_reply.assert_not_called()
        queue_pending.assert_not_called()
        mark_processed.assert_not_called()
        has_processed.assert_not_called()
        failure_doc.reference.delete.assert_not_called()

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
