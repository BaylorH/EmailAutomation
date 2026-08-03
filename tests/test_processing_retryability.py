import unittest
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
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


class _FailureSnapshot:
    def __init__(self, data):
        self._data = None if data is None else dict(data)

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FailureNode:
    def __init__(self, root, path=()):
        self._root = root
        self._path = path
        self.id = path[-1] if path else None

    def collection(self, name):
        return _FailureNode(self._root, self._path + (name,))

    def document(self, doc_id):
        encoded = str(doc_id).encode("utf-8")
        if "/" in str(doc_id) or len(encoded) > 1500:
            raise ValueError("invalid Firestore document id")
        self._root.document_ids.append(str(doc_id))
        return _FailureNode(self._root, self._path + (str(doc_id),))

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        return _FailureSnapshot(self._root.documents.get(self._path))

    def set(self, data, merge=False):
        self._root._set(self._path, data, merge=merge)

    def delete(self):
        self._root.documents.pop(self._path, None)
        self._root.deleted_paths.append(self._path)


class _FailureTransaction:
    def __init__(self, root):
        self._root = root
        self._writes = []

    def get(self, ref):
        return _FailureSnapshot(self._root.documents.get(ref._path))

    def set(self, ref, data, merge=False):
        self._writes.append((ref._path, dict(data), merge))

    def commit(self):
        for path, data, merge in self._writes:
            self._root._set(path, data, merge=merge)


class _FailureFirestore:
    def __init__(self):
        self.documents = {}
        self.document_ids = []
        self.deleted_paths = []

    def collection(self, name):
        return _FailureNode(self, (name,))

    def transaction(self):
        return _FailureTransaction(self)

    def _set(self, path, data, *, merge):
        current = dict(self.documents.get(path) or {}) if merge else {}
        current.update(dict(data))
        self.documents[path] = current


def _ordinary_terminal_retry_disposition_for_test(*_args, **_kwargs):
    return {
        "kind": "ordinary",
        "saga": None,
        "settlement": None,
        "exactSourceConfirmed": False,
    }


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

    def test_terminal_retry_disposition_trusts_only_boolean_snapshot_exists(self):
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {}
        thread_ref = MagicMock()
        thread_ref.get.return_value = snapshot
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value.document.return_value = (
            thread_ref
        )

        with patch.object(processing, "_fs", fake_fs):
            disposition = processing._terminal_retry_disposition(
                "uid-1", "thread-1", "message-1"
            )

        self.assertEqual("ordinary", disposition["kind"])
        snapshot.to_dict.assert_called_once_with()

        for exists in (False, None, MagicMock()):
            with self.subTest(exists=exists):
                snapshot = MagicMock()
                snapshot.exists = exists
                snapshot.to_dict.return_value = {}
                thread_ref = MagicMock()
                thread_ref.get.return_value = snapshot
                fake_fs = MagicMock()
                fake_fs.collection.return_value.document.return_value.collection.return_value.document.return_value = (
                    thread_ref
                )

                with patch.object(processing, "_fs", fake_fs), self.assertRaisesRegex(
                    processing.RetryableProcessingError,
                    "authoritative terminal thread is missing",
                ):
                    processing._terminal_retry_disposition(
                        "uid-1", "thread-1", "message-1"
                    )

                snapshot.to_dict.assert_not_called()

    def test_terminal_retry_disposition_untyped_match_missing_graph_is_not_exact(self):
        snapshot = MagicMock()
        snapshot.exists = True
        snapshot.to_dict.return_value = {"terminalSettlements": ["validated"]}
        thread_ref = MagicMock()
        thread_ref.get.return_value = snapshot
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value.document.return_value = (
            thread_ref
        )
        settlement = {
            "sourceMessageKey": "source-a",
            "sourceGraphMessageId": None,
            "sourceInternetMessageId": None,
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "_validate_terminal_settlement_history",
            return_value=[settlement],
        ):
            disposition = processing._terminal_retry_disposition(
                "uid-1",
                "thread-1",
                "source-a",
                graph_message_id="graph-b",
            )

        self.assertEqual("settled", disposition["kind"])
        self.assertFalse(disposition["exactSourceConfirmed"])

    def test_processing_failure_v2_key_hashes_typed_long_and_path_unsafe_ids(self):
        fake_fs = _FailureFirestore()
        graph_message_id = "AAMk/path/+=" + ("g" * 1800)
        internet_message_id = "<broker/o'hare-" + ("i" * 1800) + "@example.test>"

        with patch.object(processing, "_fs", fake_fs):
            recorded = processing._record_ai_processing_failure(
                "uid-1",
                "client-1",
                "thread/with/slash",
                internet_message_id,
                "real-shape processing failure",
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=internet_message_id,
            )

        self.assertTrue(recorded)
        failure_paths = [
            path
            for path in fake_fs.documents
            if len(path) == 4 and path[2] == "processingFailures"
        ]
        self.assertEqual(1, len(failure_paths))
        document_id = failure_paths[0][-1]
        self.assertRegex(document_id, r"^processing-failure-v2-[0-9a-f]{64}$")
        self.assertLessEqual(len(document_id.encode("utf-8")), 1500)
        self.assertNotIn("/", document_id)
        self.assertNotIn("<", document_id)
        payload = fake_fs.documents[failure_paths[0]]
        self.assertEqual(graph_message_id, payload["graphMessageId"])
        self.assertEqual(internet_message_id, payload["internetMessageId"])
        self.assertEqual(internet_message_id, payload["sourceMessageKey"])
        self.assertEqual(internet_message_id, payload["messageId"])

    def test_processing_failure_repeat_preserves_created_retry_terminality_and_attempts(self):
        fake_fs = _FailureFirestore()
        created_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        internet_message_id = "<repeat@example.test>"

        with patch.object(processing, "_fs", fake_fs):
            self.assertTrue(
                processing._record_ai_processing_failure(
                    "uid-1",
                    "client-1",
                    "thread-repeat",
                    internet_message_id,
                    "first failure",
                    graph_message_id="graph-repeat",
                    internet_message_id=internet_message_id,
                    source_message_key=internet_message_id,
                )
            )
            failure_path = next(
                path for path in fake_fs.documents if path[2] == "processingFailures"
            )
            fake_fs.documents[failure_path].update({
                "createdAt": created_at,
                "retryable": False,
                "recoveryStatus": "stale_manual_review",
                "processingAttempts": 7,
            })
            self.assertTrue(
                processing._record_ai_processing_failure(
                    "uid-1",
                    "client-1",
                    "thread-repeat",
                    internet_message_id,
                    "later scanner failure",
                    graph_message_id="graph-repeat",
                    internet_message_id=internet_message_id,
                    source_message_key=internet_message_id,
                )
            )

        retained = fake_fs.documents[failure_path]
        self.assertEqual(created_at, retained["createdAt"])
        self.assertFalse(retained["retryable"])
        self.assertEqual("stale_manual_review", retained["recoveryStatus"])
        self.assertEqual(7, retained["processingAttempts"])
        self.assertEqual(2, retained["failureOccurrences"])
        self.assertIn("lastFailedAt", retained)

    def test_processing_failure_updates_safe_legacy_doc_in_place_without_unsafe_probe(self):
        fake_fs = _FailureFirestore()
        internet_message_id = "<legacy@example.test>"
        legacy_path = (
            "users",
            "uid-1",
            "processingFailures",
            f"thread-legacy__{internet_message_id}",
        )
        fake_fs.documents[legacy_path] = {
            "threadId": "thread-legacy",
            "messageId": internet_message_id,
            "createdAt": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "retryable": False,
            "processingAttempts": 4,
        }

        with patch.object(processing, "_fs", fake_fs):
            recorded = processing._record_ai_processing_failure(
                "uid-1",
                "client-1",
                "thread-legacy",
                internet_message_id,
                "legacy failed again",
                graph_message_id="graph/unsafe-for-legacy-probe",
                internet_message_id=internet_message_id,
                source_message_key=internet_message_id,
            )

        self.assertTrue(recorded)
        self.assertEqual(
            [legacy_path],
            [path for path in fake_fs.documents if path[2] == "processingFailures"],
        )
        retained = fake_fs.documents[legacy_path]
        self.assertEqual("graph/unsafe-for-legacy-probe", retained["graphMessageId"])
        self.assertEqual(internet_message_id, retained["internetMessageId"])
        self.assertFalse(retained["retryable"])

    def test_graph_only_failure_enrichment_and_rerecord_keep_one_v2_document(self):
        fake_fs = _FailureFirestore()
        graph_message_id = "graph/resource/only"
        internet_message_id = "<enriched@example.test>"

        with patch.object(processing, "_fs", fake_fs):
            self.assertTrue(
                processing._record_ai_processing_failure(
                    "uid-1",
                    "client-1",
                    "thread-enriched",
                    graph_message_id,
                    "graph-only failure",
                    graph_message_id=graph_message_id,
                    source_message_key=graph_message_id,
                )
            )
            failure_path = next(
                path for path in fake_fs.documents if path[2] == "processingFailures"
            )
            graph_only = dict(fake_fs.documents[failure_path])
            with patch.object(
                processing,
                "_fetch_graph_message_by_id",
                return_value={
                    "id": graph_message_id,
                    "internetMessageId": internet_message_id,
                },
            ):
                _message, resolved = (
                    processing._fetch_graph_message_for_processing_failure(
                        {"Authorization": "Bearer fake"}, graph_only
                    )
                )
            # Model the retry's in-place enrichment followed by a retry failure.
            fake_fs.documents[failure_path].update({
                **resolved,
                "messageId": resolved["sourceMessageKey"],
                "processingAttempts": 1,
                "lastRetryError": "retry failed",
            })
            self.assertTrue(
                processing._record_ai_processing_failure(
                    "uid-1",
                    "client-1",
                    "thread-enriched",
                    internet_message_id,
                    "scanner observed the same failure",
                    graph_message_id=graph_message_id,
                    internet_message_id=internet_message_id,
                    source_message_key=internet_message_id,
                )
            )

        failure_paths = [
            path for path in fake_fs.documents if path[2] == "processingFailures"
        ]
        self.assertEqual([failure_path], failure_paths)
        retained = fake_fs.documents[failure_path]
        self.assertEqual("graph", retained["processingFailureIdentityKind"])
        self.assertEqual(
            graph_message_id, retained["processingFailureIdentityKey"]
        )
        self.assertEqual(internet_message_id, retained["sourceMessageKey"])
        self.assertEqual(1, retained["processingAttempts"])
        self.assertEqual(2, retained["failureOccurrences"])

    def test_processing_failure_writer_fails_closed_on_v2_legacy_collision(self):
        fake_fs = _FailureFirestore()
        graph_message_id = "graph-collision"
        internet_message_id = "<collision@example.test>"
        hashed_id = processing._processing_failure_document_id(
            "thread-collision",
            internet_message_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            source_message_key=internet_message_id,
        )
        hashed_path = (
            "users", "uid-1", "processingFailures", hashed_id
        )
        legacy_path = (
            "users",
            "uid-1",
            "processingFailures",
            f"thread-collision__{internet_message_id}",
        )
        fake_fs.documents[hashed_path] = {
            "threadId": "thread-collision",
            "retryable": True,
            "reason": "hashed",
        }
        fake_fs.documents[legacy_path] = {
            "threadId": "thread-collision",
            "retryable": False,
            "reason": "legacy",
        }
        before = {
            path: dict(data) for path, data in fake_fs.documents.items()
        }

        with patch.object(processing, "_fs", fake_fs):
            recorded = processing._record_ai_processing_failure(
                "uid-1",
                "client-1",
                "thread-collision",
                internet_message_id,
                "must not choose one duplicate",
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=internet_message_id,
            )

        self.assertFalse(recorded)
        self.assertEqual(before, fake_fs.documents)

    def test_processing_failure_writer_rejects_drifted_v2_identity_path(self):
        fake_fs = _FailureFirestore()
        graph_message_id = "graph-drift"
        internet_message_id = "<drift@example.test>"
        hashed_id = processing._processing_failure_document_id(
            "thread-drift",
            internet_message_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            source_message_key=internet_message_id,
        )
        failure_path = (
            "users", "uid-1", "processingFailures", hashed_id
        )
        fake_fs.documents[failure_path] = {
            "threadId": "thread-drift",
            "messageId": internet_message_id,
            "sourceMessageKey": internet_message_id,
            "graphMessageId": graph_message_id,
            "internetMessageId": internet_message_id,
            # This content hashes to a different v2 path than failure_path.
            "processingFailureIdentityKind": "internet",
            "processingFailureIdentityKey": internet_message_id,
            "retryable": True,
        }
        before = dict(fake_fs.documents[failure_path])

        with patch.object(processing, "_fs", fake_fs):
            recorded = processing._record_ai_processing_failure(
                "uid-1",
                "client-1",
                "thread-drift",
                internet_message_id,
                "must reject drift",
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=internet_message_id,
            )

        self.assertFalse(recorded)
        self.assertEqual(before, fake_fs.documents[failure_path])

    def test_processing_failure_writer_rejects_v2_graph_key_without_graph_alias(self):
        fake_fs = _FailureFirestore()
        graph_message_id = "graph-missing-from-content"
        internet_message_id = "<missing-graph@example.test>"
        hashed_id = processing._processing_failure_document_id(
            "thread-missing-graph",
            internet_message_id,
            graph_message_id=graph_message_id,
            internet_message_id=internet_message_id,
            source_message_key=internet_message_id,
        )
        failure_path = (
            "users", "uid-1", "processingFailures", hashed_id
        )
        fake_fs.documents[failure_path] = {
            "threadId": "thread-missing-graph",
            "messageId": internet_message_id,
            "sourceMessageKey": internet_message_id,
            "graphMessageId": None,
            "internetMessageId": internet_message_id,
            "processingFailureIdentityKind": "graph",
            "processingFailureIdentityKey": graph_message_id,
            "retryable": True,
        }

        with patch.object(processing, "_fs", fake_fs):
            recorded = processing._record_ai_processing_failure(
                "uid-1",
                "client-1",
                "thread-missing-graph",
                internet_message_id,
                "must reject missing typed alias",
                graph_message_id=graph_message_id,
                internet_message_id=internet_message_id,
                source_message_key=internet_message_id,
            )

        self.assertFalse(recorded)

    def test_processing_failure_writer_rejects_wrong_thread_legacy_record(self):
        fake_fs = _FailureFirestore()
        internet_message_id = "<wrong-thread@example.test>"
        failure_path = (
            "users",
            "uid-1",
            "processingFailures",
            f"thread-requested__{internet_message_id}",
        )
        fake_fs.documents[failure_path] = {
            "threadId": "thread-other",
            "messageId": internet_message_id,
            "retryable": True,
        }
        before = dict(fake_fs.documents[failure_path])

        with patch.object(processing, "_fs", fake_fs):
            recorded = processing._record_ai_processing_failure(
                "uid-1",
                "client-1",
                "thread-requested",
                internet_message_id,
                "must reject wrong thread",
                internet_message_id=internet_message_id,
                source_message_key=internet_message_id,
            )

        self.assertFalse(recorded)
        self.assertEqual(before, fake_fs.documents[failure_path])

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
             patch.object(processing, "_record_ai_processing_failure", return_value=True) as record_failure, \
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
            graph_message_id="graph-message-1",
            internet_message_id="<message-1@example.test>",
            source_message_key="<message-1@example.test>",
        )
        mark_processed.assert_not_called()
        self.assertEqual(0, result["processed"])
        self.assertEqual(
            [
                ("users", "uid-1", "threads", "thread-1"),
                ("users", "uid-1", "threads", "thread-1"),
            ],
            fake_fs.get_calls,
        )

    def test_scan_missing_matched_authoritative_root_fails_closed_without_effects(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        fake_fs.documents.pop(
            ("users", "uid-1", "threads", "thread-1")
        )
        response = MagicMock()
        response.json.return_value = {
            "value": [{
                "id": "graph-missing-authoritative-root",
                "internetMessageId": "<missing-authoritative-root@example.test>",
                "receivedDateTime": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            }]
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "has_processed",
        ) as has_processed, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "_save_message_to_thread",
        ) as save_message, patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
        ) as manual_guard, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "_clear_ai_processing_failure",
        ) as clear_failure, patch.object(
            processing,
            "_record_ai_processing_failure",
        ) as record_failure, patch.object(
            processing,
            "set_last_scan_iso",
        ) as set_cursor:
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("inbox_scan", result["operation"])
        self.assertIn(
            "authoritative terminal thread is missing",
            result["error"],
        )
        self.assertEqual(
            [("users", "uid-1", "threads", "thread-1")],
            fake_fs.get_calls,
        )
        has_processed.assert_not_called()
        process_message.assert_not_called()
        save_message.assert_not_called()
        manual_guard.assert_not_called()
        mark_processed.assert_not_called()
        clear_failure.assert_not_called()
        record_failure.assert_not_called()
        set_cursor.assert_not_called()

    def test_scan_returns_error_and_preserves_cursor_when_matched_failure_is_not_durable(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        response.json.return_value = {
            "value": [{
                "id": "graph-message-ledger-loss",
                "internetMessageId": "<ledger-loss@example.test>",
                "receivedDateTime": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            }]
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "process_inbox_message",
            side_effect=ValueError("processing crashed"),
        ), patch.object(
            processing,
            "_record_ai_processing_failure",
            return_value=False,
        ) as record_failure, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "set_last_scan_iso",
        ) as set_cursor:
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(1, result["failureVisibilityLost"])
        self.assertIn("processing failure", result["error"].lower())
        record_failure.assert_called_once()
        set_cursor.assert_not_called()
        mark_processed.assert_not_called()

    def test_scan_returns_error_and_preserves_cursor_when_orphan_failure_is_not_durable(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "unused", "client-1")
        response = MagicMock()
        response.json.return_value = {
            "value": [{
                "id": "graph-orphan-ledger-loss",
                "internetMessageId": "<orphan-ledger-loss@example.test>",
                "receivedDateTime": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            }]
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value=None,
        ), patch.object(
            processing,
            "process_inbox_message",
            side_effect=ValueError("orphan processing crashed"),
        ), patch.object(
            processing,
            "_record_ai_processing_failure",
            return_value=False,
        ) as record_failure, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "set_last_scan_iso",
        ) as set_cursor:
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        self.assertEqual(1, result["failureVisibilityLost"])
        record_failure.assert_called_once_with(
            "uid-1",
            "unknown",
            "orphan",
            "<orphan-ledger-loss@example.test>",
            "orphan processing crashed",
            graph_message_id="graph-orphan-ledger-loss",
            internet_message_id="<orphan-ledger-loss@example.test>",
            source_message_key="<orphan-ledger-loss@example.test>",
        )
        set_cursor.assert_not_called()
        mark_processed.assert_not_called()

    def test_scan_provisional_terminal_identity_requires_durable_review_record(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        message = {
            "id": "graph-provisional",
            "internetMessageId": "<provisional@example.test>",
            "receivedDateTime": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        }
        response = MagicMock()
        response.json.return_value = {"value": [message]}

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value={
                "kind": "settled",
                "saga": None,
                "settlement": {"sagaKey": "provisional"},
                "exactSourceConfirmed": False,
            },
        ), patch.object(
            processing,
            "_record_ai_processing_failure",
            return_value=False,
        ) as record_failure, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "set_last_scan_iso",
        ) as set_cursor:
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
            "<provisional@example.test>",
            "settled terminal source matched only an untyped alias; manual identity review is required",
            retryable=False,
            recovery_status="terminal_source_identity_unconfirmed",
            graph_message_id="graph-provisional",
            internet_message_id="<provisional@example.test>",
            source_message_key="<provisional@example.test>",
        )
        self.assertEqual("error", result["status"])
        self.assertEqual(1, result["failureVisibilityLost"])
        set_cursor.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()

    def test_manual_continuation_skip_rejects_provisional_settlement(self):
        msg = {
            "id": "graph-provisional",
            "internetMessageId": "<provisional@example.test>",
            "conversationId": "conversation-1",
        }
        with patch.object(
            processing,
            "_has_processing_failure_record",
            return_value=True,
        ), patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value={
                "kind": "settled",
                "saga": None,
                "settlement": {"sagaKey": "provisional"},
                "exactSourceConfirmed": False,
            },
        ), patch.object(
            processing,
            "_find_manual_continuation_for_inbox_retry",
        ) as continuation_guard, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed:
            skipped = processing._skip_inbox_retry_after_manual_continuation(
                "uid-1",
                {"Authorization": "Bearer fake"},
                "thread-1",
                msg,
                "<provisional@example.test>",
            )

        self.assertFalse(skipped)
        continuation_guard.assert_not_called()
        mark_processed.assert_not_called()

    def test_scan_stops_batch_when_earlier_message_cannot_be_saved(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        now = datetime.now(timezone.utc)
        earlier = {
            "id": "graph-earlier",
            "internetMessageId": "<earlier@example.test>",
            "receivedDateTime": (now - timedelta(minutes=2)).isoformat().replace(
                "+00:00", "Z"
            ),
        }
        latest = {
            "id": "graph-latest",
            "internetMessageId": "<latest@example.test>",
            "receivedDateTime": (now - timedelta(minutes=1)).isoformat().replace(
                "+00:00", "Z"
            ),
        }
        response = MagicMock()
        response.json.return_value = {"value": [earlier, latest]}

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value={
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": True,
            },
        ), patch.object(
            processing,
            "_save_message_to_thread",
            side_effect=ValueError("history persistence failed"),
        ) as save_message, patch.object(
            processing,
            "_record_ai_processing_failure",
            return_value=True,
        ) as record_failure, patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
        ) as manual_guard, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "set_last_scan_iso",
        ):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        save_message.assert_called_once_with(
            "uid-1", "thread-1", earlier, {"Authorization": "Bearer fake"}
        )
        record_failure.assert_called_once_with(
            "uid-1",
            "client-1",
            "thread-1",
            "<earlier@example.test>",
            "Conversation history persistence failed: history persistence failed",
            graph_message_id="graph-earlier",
            internet_message_id="<earlier@example.test>",
            source_message_key="<earlier@example.test>",
        )
        manual_guard.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        self.assertEqual("healthy", result["status"])
        self.assertEqual(0, result["processed"])

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
            graph_message_id="graph-message-1",
            internet_message_id="<message-1@example.test>",
            source_message_key="<message-1@example.test>",
        )
        mark_processed.assert_called_once_with("uid-1", "<message-1@example.test>")
        self.assertEqual(0, result["processed"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual(
            [
                ("users", "uid-1", "threads", "thread-1"),
                ("users", "uid-1", "threads", "thread-1"),
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
        self.assertEqual(
            {
                processing._processing_failure_document_id(
                    "thread-1", "message-1"
                ),
                "thread-1__message-1",
            },
            {call.args[0] for call in failure_doc.call_args_list},
        )
        self.assertEqual(2, failure_doc.return_value.delete.call_count)

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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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

    def test_retry_processing_failure_real_shape_gets_only_typed_graph_resource_id(self):
        graph_message_id = "AAMk/resource/with+slash="
        internet_message_id = "<real-shape@example.test>"
        failure_doc = MagicMock()
        failure_doc.id = "processing-failure-v2-real-shape"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": internet_message_id,
            "sourceMessageKey": internet_message_id,
            "graphMessageId": graph_message_id,
            "internetMessageId": internet_message_id,
            "retryable": True,
            "processingAttempts": 0,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )
        graph_response = MagicMock()
        graph_response.status_code = 200
        graph_message = {
            "id": graph_message_id,
            "internetMessageId": internet_message_id,
            "conversationId": "conversation-1",
        }
        graph_response.json.return_value = graph_message
        requests_seen = []

        def fake_get(url, **kwargs):
            requests_seen.append((url, kwargs))
            return graph_response

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value={
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": True,
            },
        ) as disposition, patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_find_existing_retry_artifact_for_message",
            return_value=None,
        ), patch.object(
            processing,
            "_find_sent_item_continuing_conversation",
            return_value=None,
        ), patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=lambda request_fn: request_fn(),
        ), patch.object(
            processing.requests,
            "get",
            side_effect=fake_get,
        ), patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ):
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(1, result["succeeded"])
        self.assertEqual(1, len(requests_seen))
        self.assertTrue(
            requests_seen[0][0].endswith(
                f"/me/messages/{quote(graph_message_id, safe='')}"
            )
        )
        self.assertNotIn(internet_message_id, requests_seen[0][0])
        self.assertEqual(
            graph_message_id,
            disposition.call_args_list[1].kwargs["graph_message_id"],
        )
        self.assertEqual(
            internet_message_id,
            disposition.call_args_list[1].kwargs["internet_message_id"],
        )
        process_message.assert_called_once_with(
            "uid-1",
            {"Authorization": "Bearer fake"},
            graph_message,
        )

    def test_retry_legacy_internet_only_uses_exact_escaped_filter_then_typed_disposition(self):
        internet_message_id = "<legacy.o'hare@example.test>"
        graph_message_id = "AAMk-legacy-resolved"
        failure_doc = MagicMock()
        failure_doc.id = "legacy-safe-id"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-legacy",
            "messageId": internet_message_id,
            "retryable": True,
            "processingAttempts": 0,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )
        graph_message = {
            "id": graph_message_id,
            "internetMessageId": internet_message_id,
            "conversationId": "conversation-legacy",
        }
        graph_response = MagicMock()
        graph_response.status_code = 200
        graph_response.json.return_value = {"value": [graph_message]}
        requests_seen = []

        def fake_get(url, **kwargs):
            requests_seen.append((url, kwargs))
            return graph_response

        dispositions = [
            {
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": False,
            },
            {
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": False,
            },
        ]
        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "_terminal_retry_disposition",
            side_effect=dispositions,
        ) as disposition, patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_find_existing_retry_artifact_for_message",
            return_value=None,
        ), patch.object(
            processing,
            "_find_sent_item_continuing_conversation",
            return_value=None,
        ), patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=lambda request_fn: request_fn(),
        ), patch.object(
            processing.requests,
            "get",
            side_effect=fake_get,
        ), patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ):
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(1, result["succeeded"])
        self.assertEqual(1, len(requests_seen))
        lookup_url, lookup_kwargs = requests_seen[0]
        self.assertTrue(lookup_url.endswith("/me/mailFolders/Inbox/messages"))
        self.assertEqual(
            "internetMessageId eq '<legacy.o''hare@example.test>'",
            lookup_kwargs["params"]["$filter"],
        )
        self.assertEqual(2, lookup_kwargs["params"]["$top"])
        self.assertEqual(
            graph_message_id,
            disposition.call_args_list[1].kwargs["graph_message_id"],
        )
        self.assertEqual(
            internet_message_id,
            disposition.call_args_list[1].kwargs["internet_message_id"],
        )
        migrated = failure_doc.reference.set.call_args_list[0].args[0]
        self.assertEqual(graph_message_id, migrated["graphMessageId"])
        self.assertEqual(internet_message_id, migrated["internetMessageId"])
        self.assertEqual(internet_message_id, migrated["sourceMessageKey"])
        process_message.assert_called_once()
        failure_doc.reference.delete.assert_called_once()

    def test_retry_legacy_internet_filter_fails_closed_on_multiple_exact_matches(self):
        internet_message_id = "<ambiguous@example.test>"
        failure_doc = MagicMock()
        failure_doc.id = "legacy-ambiguous"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-ambiguous",
            "messageId": internet_message_id,
            "retryable": True,
            "processingAttempts": 0,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )
        response = MagicMock()
        response.json.return_value = {
            "value": [
                {"id": "graph-a", "internetMessageId": internet_message_id},
                {"id": "graph-b", "internetMessageId": internet_message_id},
            ]
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value={
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": False,
            },
        ), patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_find_existing_retry_artifact_for_message",
            return_value=None,
        ), patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=lambda request_fn: request_fn(),
        ), patch.object(
            processing.requests,
            "get",
            return_value=response,
        ), patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ):
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(1, result["failed"])
        process_message.assert_not_called()
        failure_doc.reference.delete.assert_not_called()
        update_payload = failure_doc.reference.set.call_args.args[0]
        self.assertIn("multiple exact messages", update_payload["lastRetryError"])

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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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

    def test_retry_processing_failures_routes_exact_terminal_saga_before_generic_guards(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__graph-terminal-source"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "graph-terminal-source",
            "retryable": False,
            "processingAttempts": 99,
            "recoveryStatus": "blocked_existing_outbound_artifact",
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )
        graph_message = {
            "id": "graph-terminal-source",
            "internetMessageId": "<terminal-source@example.test>",
            "conversationId": "conversation-terminal-source",
        }
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="blocked",
            reason="client_stopped_by_user",
            client_data={"status": "stopped"},
            metadata={"terminal": True, "stopKind": "user_stop"},
        )

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(
                 processing,
                 "_terminal_retry_disposition",
                 return_value={
                     "kind": "active",
                     "saga": {"sagaKey": "terminal-saga-key"},
                     "settlement": None,
                     "exactSourceConfirmed": True,
                 },
                 create=True,
             ) as disposition_lookup, \
             patch.object(processing, "has_processed", return_value=True) as has_processed, \
             patch.object(
                 processing,
                 "_find_existing_retry_artifact_for_message",
                 return_value={"collection": "threads/thread-1/handledEvents"},
             ) as artifact_guard, \
             patch.object(
                 processing,
                 "_fetch_graph_message_by_id",
                 return_value=graph_message,
             ), \
             patch.object(
                 processing,
                 "_find_sent_item_continuing_conversation",
                 return_value={"id": "manual-continuation"},
             ) as continuation_guard, \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 1, "succeeded": 1, "failed": 0, "skipped": 0},
            result,
        )
        disposition_lookup.assert_called()
        has_processed.assert_not_called()
        artifact_guard.assert_not_called()
        continuation_guard.assert_not_called()
        process_message.assert_called_once_with(
            "uid-1",
            {"Authorization": "Bearer fake"},
            graph_message,
        )
        mark_processed.assert_any_call("uid-1", "graph-terminal-source")
        mark_processed.assert_any_call("uid-1", "<terminal-source@example.test>")
        failure_doc.reference.delete.assert_called_once()

    def test_retry_processing_failures_clears_exact_settlement_before_all_generic_guards(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__settled-generation-3"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "settled-generation-3",
            "sourceMessageKey": "settled-generation-3",
            "graphMessageId": "settled-generation-3",
            "internetMessageId": "<settled-generation-3@example.test>",
            "retryable": True,
            "processingAttempts": 1,
            "recoveryStatus": "blocked_existing_outbound_artifact",
        }
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

        graph_message = {
            "id": "settled-generation-3",
            "internetMessageId": "<settled-generation-3@example.test>",
            "conversationId": "conversation-settled-generation-3",
        }
        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value={
                "kind": "settled",
                "saga": None,
                "settlement": {
                    "settlementOrdinal": 3,
                    "sagaKey": "settled-generation-3",
                },
                "exactSourceConfirmed": True,
            },
        ), patch.object(
            processing,
            "has_processed",
            return_value=True,
        ) as has_processed, patch.object(
            processing,
            "_find_existing_retry_artifact_for_message",
        ) as artifact_guard, patch.object(
            processing,
            "_fetch_graph_message_by_id",
            return_value=graph_message,
        ) as graph_fetch, patch.object(
            processing,
            "_find_sent_item_continuing_conversation",
        ) as continuation_guard, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(
            {"checked": 1, "retried": 0, "succeeded": 0, "failed": 0, "skipped": 1},
            result,
        )
        self.campaign_decision.assert_not_called()
        has_processed.assert_not_called()
        artifact_guard.assert_not_called()
        graph_fetch.assert_not_called()
        continuation_guard.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        failure_doc.reference.set.assert_not_called()
        failure_doc.reference.delete.assert_called_once()

    def test_retry_processing_failure_never_deletes_provisional_settlement_on_alias_contradiction(self):
        failure_doc = MagicMock()
        failure_doc.id = "thread-1__provisional-settlement"
        failure_doc.to_dict.return_value = {
            "clientId": "client-1",
            "threadId": "thread-1",
            "messageId": "graph-generation-a",
            "retryable": True,
            "processingAttempts": 0,
        }
        failures_collection = MagicMock()
        failures_collection.limit.return_value.stream.return_value = [failure_doc]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value.collection.return_value = (
            failures_collection
        )
        graph_message = {
            "id": "graph-generation-b",
            "internetMessageId": "<internet-generation-a@example.test>",
            "conversationId": "conversation-conflict",
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "_terminal_retry_disposition",
            side_effect=[
                {
                    "kind": "settled",
                    "saga": None,
                    "settlement": {"sagaKey": "generation-a"},
                    "exactSourceConfirmed": False,
                },
                processing.RetryableProcessingError(
                    "terminal retry disposition received contradictory source aliases"
                ),
            ],
        ), patch.object(
            processing,
            "_fetch_graph_message_by_id",
            return_value=graph_message,
        ) as graph_fetch, patch.object(
            processing,
            "has_processed",
        ) as processed_guard, patch.object(
            processing,
            "_find_existing_retry_artifact_for_message",
        ) as artifact_guard, patch.object(
            processing,
            "_find_sent_item_continuing_conversation",
        ) as continuation_guard, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed:
            result = processing.retry_processing_failures(
                "uid-1",
                {"Authorization": "Bearer fake"},
            )

        self.assertEqual(0, result["failed"])
        self.assertEqual(1, result["skipped"])
        self.assertEqual(0, result["succeeded"])
        failure_doc.reference.delete.assert_not_called()
        failure_doc.reference.set.assert_called_once()
        self.assertEqual(
            "terminal_source_identity_unconfirmed",
            failure_doc.reference.set.call_args.args[0]["recoveryStatus"],
        )
        graph_fetch.assert_not_called()
        processed_guard.assert_not_called()
        artifact_guard.assert_not_called()
        continuation_guard.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()

    def test_scan_routes_processed_exact_terminal_source_before_batch_manual_guard(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        received_now = datetime.now(timezone.utc)
        terminal_msg = {
            "id": "graph-terminal-source",
            "internetMessageId": "<terminal-source@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": (
                received_now - timedelta(minutes=2)
            ).isoformat().replace("+00:00", "Z"),
            "conversationId": "conversation-1",
        }
        later_msg = {
            "id": "graph-later-source",
            "internetMessageId": "<later-source@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": (
                received_now - timedelta(minutes=1)
            ).isoformat().replace("+00:00", "Z"),
            "conversationId": "conversation-1",
        }
        response.json.return_value = {"value": [terminal_msg, later_msg]}

        def retry_disposition(_user_id, _thread_id, *message_ids, **typed_ids):
            source_values = [*message_ids, *typed_ids.values()]
            if any("terminal-source" in str(value or "") for value in source_values):
                return {
                    "kind": "active",
                    "saga": {"sagaKey": "terminal-saga-key"},
                    "settlement": None,
                    "exactSourceConfirmed": True,
                }
            return {"kind": "ordinary", "saga": None, "settlement": None}

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "exponential_backoff_request", return_value=response), \
             patch.object(
                 processing,
                 "has_processed",
                 side_effect=lambda _uid, key: key == "<terminal-source@example.test>",
             ), \
             patch.object(processing, "_match_message_to_thread", return_value="thread-1"), \
             patch.object(
                 processing,
                 "_terminal_retry_disposition",
                 side_effect=retry_disposition,
                 create=True,
             ), \
             patch.object(processing, "_has_processing_failure_record", return_value=True), \
             patch.object(
                 processing,
                 "find_sent_conversation_continuation_for_retry",
                 return_value={"id": "manual-continuation"},
             ), \
             patch.object(processing, "process_inbox_message") as process_message, \
             patch.object(processing, "mark_processed") as mark_processed, \
             patch.object(processing, "_record_processing_failure_blocked_by_manual_continuation"), \
             patch.object(processing, "set_last_scan_iso"):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        process_message.assert_called_once_with(
            "uid-1",
            {"Authorization": "Bearer fake"},
            terminal_msg,
        )
        mark_processed.assert_any_call("uid-1", "<terminal-source@example.test>")
        self.assertEqual(1, result["processed"])
        self.assertEqual(1, result["skipped"])

    def test_scan_stops_same_thread_batch_when_exact_terminal_recovery_fails(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        received_now = datetime.now(timezone.utc)
        terminal_msg = {
            "id": "graph-terminal-source",
            "internetMessageId": "<terminal-source@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": (
                received_now - timedelta(minutes=2)
            ).isoformat().replace("+00:00", "Z"),
            "conversationId": "conversation-1",
        }
        later_msg = {
            "id": "graph-later-source",
            "internetMessageId": "<later-source@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": (
                received_now - timedelta(minutes=1)
            ).isoformat().replace("+00:00", "Z"),
            "conversationId": "conversation-1",
        }
        response.json.return_value = {"value": [terminal_msg, later_msg]}

        def retry_disposition(_user_id, _thread_id, *message_ids, **typed_ids):
            source_values = [*message_ids, *typed_ids.values()]
            if any("terminal-source" in str(value or "") for value in source_values):
                return {
                    "kind": "active",
                    "saga": {"sagaKey": "terminal-saga-key"},
                    "settlement": None,
                    "exactSourceConfirmed": True,
                }
            return {"kind": "ordinary", "saga": None, "settlement": None}

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "exponential_backoff_request", return_value=response), \
             patch.object(processing, "has_processed", return_value=False), \
             patch.object(processing, "_match_message_to_thread", return_value="thread-1"), \
             patch.object(
                 processing,
                 "_terminal_retry_disposition",
                 side_effect=retry_disposition,
                 create=True,
             ), \
             patch.object(processing, "_has_processing_failure_record", return_value=False), \
             patch.object(
                 processing,
                 "process_inbox_message",
                 side_effect=[
                     processing.RetryableProcessingError("exact recovery still failing"),
                     None,
                 ],
             ) as process_message, \
             patch.object(processing, "mark_processed") as mark_processed, \
             patch.object(processing, "_record_ai_processing_failure", return_value=True) as record_failure, \
             patch.object(processing, "set_last_scan_iso"):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        process_message.assert_called_once_with(
            "uid-1",
            {"Authorization": "Bearer fake"},
            terminal_msg,
        )
        mark_processed.assert_not_called()
        record_failure.assert_called_once()
        self.assertEqual(0, result["processed"])

    def test_scan_fails_closed_before_skip_or_batch_save_on_contradictory_terminal_aliases(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        response.json.return_value = {
            "value": [{
                "id": "graph-generation-b",
                "internetMessageId": "<internet-generation-a@example.test>",
                "subject": "RE: 4402 Rex Rd",
                "receivedDateTime": datetime.now(timezone.utc).isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "conversationId": "conversation-conflict",
            }]
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "_terminal_retry_disposition",
            side_effect=processing.RetryableProcessingError(
                "terminal retry disposition received contradictory source aliases"
            ),
        ), patch.object(
            processing,
            "has_processed",
        ) as processed_guard, patch.object(
            processing,
            "_save_message_to_thread",
        ) as save_batched, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "set_last_scan_iso",
        ) as set_last_scan:
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        self.assertEqual("error", result["status"])
        processed_guard.assert_not_called()
        save_batched.assert_not_called()
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        set_last_scan.assert_not_called()

    def test_scan_single_active_terminal_source_bypasses_manual_continuation(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        active_msg = {
            "id": "graph-active-generation-8",
            "internetMessageId": "<active-generation-8@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": datetime.now(timezone.utc).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "conversationId": "conversation-active-8",
        }
        response.json.return_value = {"value": [active_msg]}
        active_disposition = {
            "kind": "active",
            "saga": {"sagaKey": "active-generation-8"},
            "settlement": None,
            "exactSourceConfirmed": True,
        }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "_terminal_retry_disposition",
            return_value=active_disposition,
        ), patch.object(
            processing,
            "has_processed",
            return_value=True,
        ), patch.object(
            processing,
            "_has_processing_failure_record",
            return_value=True,
        ), patch.object(
            processing,
            "find_sent_conversation_continuation_for_retry",
        ) as continuation_guard, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ), patch.object(
            processing,
            "_clear_ai_processing_failure",
        ), patch.object(
            processing,
            "set_last_scan_iso",
        ):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=1,
            )

        process_message.assert_called_once_with(
            "uid-1",
            {"Authorization": "Bearer fake"},
            active_msg,
        )
        continuation_guard.assert_not_called()
        self.assertEqual(1, result["processed"])

    def test_scan_filters_settled_generation_before_batch_save_and_processes_new_source(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        now = datetime.now(timezone.utc)
        settled_msg = {
            "id": "graph-settled-generation-7",
            "internetMessageId": "<settled-generation-7@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": (now - timedelta(minutes=1)).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "conversationId": "conversation-1",
        }
        new_msg = {
            "id": "graph-new-generation-9",
            "internetMessageId": "<new-generation-9@example.test>",
            "subject": "RE: 4402 Rex Rd",
            "receivedDateTime": now.isoformat().replace("+00:00", "Z"),
            "conversationId": "conversation-1",
        }
        response.json.return_value = {"value": [settled_msg, new_msg]}

        def disposition(_uid, _thread, *_aliases, **typed):
            if typed.get("graph_message_id") == settled_msg["id"]:
                return {
                    "kind": "settled",
                    "saga": None,
                    "settlement": {
                        "settlementOrdinal": 7,
                        "sagaKey": "settled-generation-7",
                    },
                    "exactSourceConfirmed": True,
                }
            return {
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": True,
            }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "_terminal_retry_disposition",
            side_effect=disposition,
        ), patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_save_message_to_thread",
        ) as save_batched, patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
            return_value=False,
        ) as manual_guard, patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(
            processing,
            "_record_ai_processing_failure",
        ) as record_failure, patch.object(
            processing,
            "_clear_ai_processing_failure",
        ), patch.object(
            processing,
            "set_last_scan_iso",
        ):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        save_batched.assert_not_called()
        manual_guard.assert_called_once()
        self.assertIs(new_msg, manual_guard.call_args.args[3])
        process_message.assert_called_once_with(
            "uid-1",
            {"Authorization": "Bearer fake"},
            new_msg,
        )
        record_failure.assert_not_called()
        self.assertNotIn(
            ("uid-1", settled_msg["internetMessageId"]),
            [call.args for call in mark_processed.call_args_list],
        )
        self.assertEqual(1, result["processed"])
        self.assertEqual(1, result["skipped"])

    def test_scan_runs_newer_active_before_older_ordinary_same_thread_message(self):
        fake_fs = _ThreadLookupFirestore("uid-1", "thread-1", "client-1")
        response = MagicMock()
        now = datetime.now(timezone.utc)
        ordinary_msg = {
            "id": "graph-ordinary-older",
            "internetMessageId": "<ordinary-older@example.test>",
            "receivedDateTime": (now - timedelta(minutes=2)).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "conversationId": "conversation-1",
        }
        active_msg = {
            "id": "graph-active-newer",
            "internetMessageId": "<active-newer@example.test>",
            "receivedDateTime": (now - timedelta(minutes=1)).isoformat().replace(
                "+00:00",
                "Z",
            ),
            "conversationId": "conversation-1",
        }
        response.json.return_value = {"value": [ordinary_msg, active_msg]}

        def disposition(_uid, _thread, *_aliases, **typed):
            if typed.get("graph_message_id") == active_msg["id"]:
                return {
                    "kind": "active",
                    "saga": {"sagaKey": "active-newer"},
                    "settlement": None,
                    "exactSourceConfirmed": True,
                }
            return {
                "kind": "ordinary",
                "saga": None,
                "settlement": None,
                "exactSourceConfirmed": True,
            }

        with patch.object(processing, "_fs", fake_fs), patch.object(
            processing,
            "exponential_backoff_request",
            return_value=response,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(
            processing,
            "_terminal_retry_disposition",
            side_effect=disposition,
        ), patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
            return_value=False,
        ), patch.object(
            processing,
            "process_inbox_message",
        ) as process_message, patch.object(
            processing,
            "mark_processed",
        ), patch.object(
            processing,
            "_clear_ai_processing_failure",
        ), patch.object(
            processing,
            "set_last_scan_iso",
        ):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        self.assertEqual(
            [active_msg, ordinary_msg],
            [call.args[2] for call in process_message.call_args_list],
        )
        self.assertEqual(2, result["processed"])

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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
        self.assertEqual(2, failure_doc.reference.set.call_count)
        update_payload = failure_doc.reference.set.call_args_list[-1].args[0]
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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
            processing,
            "_terminal_retry_disposition",
            side_effect=_ordinary_terminal_retry_disposition_for_test,
        ), patch.object(
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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
        empty_thread_ref = MagicMock()
        empty_thread_ref.get.return_value.exists = False
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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
        graph_response.json.return_value = {"value": [{
            "id": "graph-message-1",
            "internetMessageId": "<internet-message-1@example.test>",
            "conversationId": "conversation-1",
        }]}

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
        graph_response.json.return_value = {"value": [{
            "id": "graph-message-1",
            "internetMessageId": "<internet-message-1@example.test>",
            "conversationId": "conversation-1",
        }]}
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
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
        graph_response.json.return_value = {"value": [{
            "id": "graph-message-1",
            "internetMessageId": "<internet-message-1@example.test>",
            "conversationId": "conversation-1",
        }]}

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
        empty_thread_ref = MagicMock()
        empty_thread_ref.get.return_value.exists = False

        user_doc = MagicMock()
        user_doc.collection.side_effect = lambda name: {
            "processingFailures": failures_collection,
            "outbox": unreadable_outbox,
            "pendingResponses": empty_collection,
            "deadLetterQueue": empty_collection,
            "actionAudit": empty_collection,
            "clients": MagicMock(document=MagicMock(return_value=MagicMock(collection=MagicMock(return_value=empty_collection)))),
            "threads": MagicMock(document=MagicMock(return_value=empty_thread_ref)),
        }[name]
        fake_fs = MagicMock()
        fake_fs.collection.return_value.document.return_value = user_doc

        with patch.object(processing, "_fs", fake_fs), \
             patch.object(processing, "_terminal_retry_disposition", side_effect=_ordinary_terminal_retry_disposition_for_test), \
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
