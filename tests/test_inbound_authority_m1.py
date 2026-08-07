import os
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import followup, processing


class _ThreadSnapshot:
    def __init__(self, data=None, *, exists=True):
        self._data = deepcopy(data or {})
        self.exists = exists

    def to_dict(self):
        return deepcopy(self._data)


class _TransactionConflict(RuntimeError):
    pass


class _ThreadTransaction:
    def __init__(self, store):
        self.store = store
        self.read_version = None

    def update(self, ref, payload):
        if self.read_version != self.store.version:
            raise _TransactionConflict("thread changed after transaction read")
        if ref.update_error:
            raise ref.update_error
        ref._apply_update(payload)


def _retrying_transactional(fn):
    """Model Firestore's retry when a document changes after transaction read."""

    def wrapped(transaction, *args, **kwargs):
        current_transaction = transaction
        while True:
            try:
                return fn(current_transaction, *args, **kwargs)
            except _TransactionConflict:
                current_transaction = _ThreadTransaction(transaction.store)

    return wrapped


class _ThreadFirestore:
    """Small stateful double for one thread document."""

    def __init__(
        self,
        data=None,
        *,
        exists=True,
        update_error=None,
        transition_after_first_read=None,
    ):
        self.data = deepcopy(data or {})
        self.exists = exists
        self.update_error = update_error
        self.transition_after_first_read = deepcopy(transition_after_first_read)
        self.transition_fired = False
        self.version = 0
        self.read_count = 0
        self.updates = []

    def collection(self, _name):
        return self

    def document(self, _doc_id):
        return self

    def get(self, transaction=None):
        self.read_count += 1
        snapshot = _ThreadSnapshot(self.data, exists=self.exists)
        if transaction is not None:
            transaction.read_version = self.version
        if self.transition_after_first_read and not self.transition_fired:
            self.data.update(deepcopy(self.transition_after_first_read))
            self.version += 1
            self.transition_fired = True
        return snapshot

    def update(self, payload):
        if self.update_error:
            raise self.update_error
        self._apply_update(payload)

    def _apply_update(self, payload):
        self.updates.append(dict(payload))
        self.data.update(payload)
        self.version += 1

    def transaction(self):
        return _ThreadTransaction(self)


class CanonicalInboundMarkerTests(unittest.TestCase):
    def _cancel(self, thread_data=None, *, exists=True, update_error=None):
        fake_fs = _ThreadFirestore(
            thread_data,
            exists=exists,
            update_error=update_error,
        )
        with patch.object(followup, "_fs", fake_fs), patch(
            "google.cloud.firestore.transactional",
            _retrying_transactional,
        ):
            result = followup.cancel_followup_on_response("uid-1", "thread-1")
        return fake_fs, result

    def test_disabled_followups_still_record_canonical_inbound_markers(self):
        fake_fs, _ = self._cancel({
            "status": "active",
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": False},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertTrue(update["hasInboundReply"])
        self.assertIs(update["lastInboundAt"], followup.SERVER_TIMESTAMP)
        self.assertIs(update["updatedAt"], followup.SERVER_TIMESTAMP)
        self.assertNotIn("followUpStatus", update)
        self.assertNotIn("followUpConfig.pausedAt", update)

    def test_terminal_followup_state_keeps_terminal_fields_and_updates_markers(self):
        fake_fs, _ = self._cancel({
            "status": "completed",
            "statusReason": "all_fields_gathered",
            "followUpStatus": "completed",
            "followUpConfig": {"enabled": True},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertEqual(
            {"hasInboundReply", "lastInboundAt", "updatedAt"},
            set(update),
        )
        self.assertEqual("completed", fake_fs.data["status"])
        self.assertEqual("all_fields_gathered", fake_fs.data["statusReason"])
        self.assertEqual("completed", fake_fs.data["followUpStatus"])

    def test_archived_thread_does_not_rewrite_stale_waiting_followup_state(self):
        fake_fs, _ = self._cancel({
            "status": "archived",
            "statusReason": "archived_by_user",
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": True},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertEqual(
            {"hasInboundReply", "lastInboundAt", "updatedAt"},
            set(update),
        )
        self.assertEqual("archived", fake_fs.data["status"])
        self.assertEqual("waiting", fake_fs.data["followUpStatus"])

    def test_enabled_waiting_followup_records_markers_and_pauses_sequence(self):
        fake_fs, _ = self._cancel({
            "status": "active",
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": True},
        })

        self.assertEqual(1, len(fake_fs.updates))
        update = fake_fs.updates[0]
        self.assertTrue(update["hasInboundReply"])
        self.assertEqual("paused", update["followUpStatus"])
        self.assertIs(update["followUpConfig.pausedAt"], followup.SERVER_TIMESTAMP)
        self.assertEqual(
            "mid_conversation",
            update["followUpConfig.conversationStage"],
        )

    def test_enabled_nonwaiting_followup_states_only_receive_markers(self):
        for followup_status in (
            None,
            "needs_review",
            "paused",
            "completed",
            "max_reached",
            "stopped",
        ):
            with self.subTest(followup_status=followup_status):
                thread_data = {
                    "status": "active",
                    "followUpConfig": {"enabled": True},
                }
                if followup_status is not None:
                    thread_data["followUpStatus"] = followup_status

                fake_fs, _ = self._cancel(thread_data)

                self.assertEqual(1, len(fake_fs.updates))
                self.assertEqual(
                    {"hasInboundReply", "lastInboundAt", "updatedAt"},
                    set(fake_fs.updates[0]),
                )
                self.assertEqual(
                    followup_status,
                    fake_fs.data.get("followUpStatus"),
                )

    def test_concurrent_terminal_or_review_transition_wins_over_stale_pause(self):
        transitions = (
            {
                "status": "stopped",
                "statusReason": "stopped_by_user",
                "followUpStatus": "stopped",
            },
            {
                "status": "completed",
                "statusReason": "all_fields_gathered",
                "followUpStatus": "completed",
            },
            {
                "status": "action_needed",
                "statusReason": "followup_send_guard_failed",
                "followUpStatus": "needs_review",
            },
        )
        for transition in transitions:
            with self.subTest(transition=transition["followUpStatus"]):
                fake_fs = _ThreadFirestore(
                    {
                        "status": "active",
                        "followUpStatus": "waiting",
                        "followUpConfig": {"enabled": True},
                    },
                    transition_after_first_read=transition,
                )

                with patch.object(followup, "_fs", fake_fs), patch(
                    "google.cloud.firestore.transactional",
                    _retrying_transactional,
                ):
                    followup.cancel_followup_on_response("uid-1", "thread-1")

                self.assertGreaterEqual(fake_fs.read_count, 2)
                self.assertEqual(transition["status"], fake_fs.data["status"])
                self.assertEqual(
                    transition["followUpStatus"],
                    fake_fs.data["followUpStatus"],
                )
                self.assertEqual(
                    transition["statusReason"],
                    fake_fs.data["statusReason"],
                )
                self.assertTrue(fake_fs.data["hasInboundReply"])
                self.assertIn("lastInboundAt", fake_fs.data)
                self.assertEqual(
                    {"hasInboundReply", "lastInboundAt", "updatedAt"},
                    set(fake_fs.updates[-1]),
                )

    def test_missing_thread_is_a_noop(self):
        fake_fs, result = self._cancel(exists=False)

        self.assertEqual([], fake_fs.updates)
        self.assertIsNone(result)

    def test_marker_write_failure_surfaces_to_the_inbox_retry_boundary(self):
        with self.assertRaisesRegex(RuntimeError, "marker write unavailable"):
            self._cancel(
                {
                    "status": "active",
                    "followUpStatus": "waiting",
                    "followUpConfig": {"enabled": False},
                },
                update_error=RuntimeError("marker write unavailable"),
            )


class TourPhraseClassifierTests(unittest.TestCase):
    def test_set_a_tour_and_set_up_a_tour_are_explicit_tour_requests(self):
        for phrase in (
            "We can set a tour for Tuesday at 10:30.",
            "We can set up a tour for Tuesday at 10:30.",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(
                    processing._looks_like_explicit_tour_offer_or_request(phrase)
                )


class _GraphResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return deepcopy(self._payload)


class _GraphIdentityResponse(_GraphResponse):
    def __init__(self, status_code, payload=None):
        super().__init__(payload or {})
        self.status_code = status_code


class BatchedInboundAuthorityTests(unittest.TestCase):
    def test_single_message_identity_failure_escapes_before_canonical_authority(self):
        msg = {
            "id": "graph-single",
            "internetMessageId": "<single@example.test>",
            "conversationId": "conversation-1",
            "subject": "RE: 912 Gemini St",
            "from": {"emailAddress": {"address": "broker@example.test"}},
            "sender": {"emailAddress": {"address": "broker@example.test"}},
            "receivedDateTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "bodyPreview": "The property has 600A power.",
            "hasAttachments": False,
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": "<outbound@example.test>"},
            ],
        }
        full_body = _GraphResponse({
            "body": {"contentType": "Text", "content": "The property has 600A power."},
            "hasAttachments": False,
        })

        with patch.object(
            processing,
            "exponential_backoff_request",
            return_value=full_body,
        ), patch.object(
            processing,
            "_resolve_current_mailbox_email",
            side_effect=processing.RetryableProcessingError(
                "mailbox identity unavailable"
            ),
        ), patch.object(processing, "lookup_thread_by_message_id") as lookup_thread, patch.object(
            processing,
            "save_message",
        ) as save_message, patch(
            "email_automation.followup.cancel_followup_on_response",
        ) as record_authority:
            with self.assertRaisesRegex(
                processing.RetryableProcessingError,
                "mailbox identity unavailable",
            ):
                processing.process_inbox_message(
                    "uid-1",
                    {"Authorization": "Bearer fake"},
                    msg,
                )

        lookup_thread.assert_not_called()
        save_message.assert_not_called()
        record_authority.assert_not_called()

    def test_distinct_singleton_threads_share_one_mailbox_identity_resolution(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        messages = [
            {
                "id": "graph-one",
                "internetMessageId": "<one@example.test>",
                "conversationId": "conversation-1",
                "subject": "RE: 912 Gemini St",
                "from": {"emailAddress": {"address": "broker-one@example.test"}},
                "receivedDateTime": now.isoformat().replace("+00:00", "Z"),
            },
            {
                "id": "graph-two",
                "internetMessageId": "<two@example.test>",
                "conversationId": "conversation-2",
                "subject": "RE: 4402 Rex Rd",
                "from": {"emailAddress": {"address": "broker-two@example.test"}},
                "receivedDateTime": (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            },
        ]
        inbox_response = _GraphResponse({"value": messages})

        with patch.object(
            processing,
            "exponential_backoff_request",
            return_value=inbox_response,
        ), patch.object(processing, "has_processed", return_value=False), patch.object(
            processing,
            "_match_message_to_thread",
            side_effect=["thread-1", "thread-2"],
        ), patch.object(
            processing,
            "_resolve_current_mailbox_email",
            return_value="operator@example.test",
        ) as resolve_identity, patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
            return_value=False,
        ), patch.object(
            processing,
            "process_inbox_message",
            return_value=None,
        ) as process_message, patch.object(processing, "mark_processed"), patch.object(
            processing,
            "_clear_ai_processing_failure",
        ), patch.object(
            processing,
            "set_last_scan_iso",
        ), patch.object(processing.time, "sleep"):
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        self.assertEqual("healthy", result["status"])
        resolve_identity.assert_called_once_with({"Authorization": "Bearer fake"})
        self.assertEqual(2, process_message.call_count)
        for call in process_message.call_args_list:
            self.assertEqual(
                "operator@example.test",
                call.kwargs["authenticated_mailbox_email"],
            )

    def test_singleton_scan_identity_failure_leaves_every_message_retryable(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        messages = [
            {
                "id": "graph-one",
                "internetMessageId": "<one@example.test>",
                "conversationId": "conversation-1",
                "subject": "RE: 912 Gemini St",
                "receivedDateTime": now.isoformat().replace("+00:00", "Z"),
            },
            {
                "id": "graph-two",
                "internetMessageId": "<two@example.test>",
                "conversationId": "conversation-2",
                "subject": "RE: 4402 Rex Rd",
                "receivedDateTime": (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            },
        ]

        with patch.object(
            processing,
            "exponential_backoff_request",
            return_value=_GraphResponse({"value": messages}),
        ), patch.object(processing, "has_processed", return_value=False), patch.object(
            processing,
            "_match_message_to_thread",
            side_effect=["thread-1", "thread-2"],
        ), patch.object(
            processing,
            "_resolve_current_mailbox_email",
            side_effect=processing.RetryableProcessingError(
                "mailbox identity unavailable"
            ),
        ) as resolve_identity, patch.object(
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
                top=2,
            )

        self.assertEqual("error", result["status"])
        self.assertTrue(result["retryable"])
        resolve_identity.assert_called_once_with({"Authorization": "Bearer fake"})
        process_message.assert_not_called()
        mark_processed.assert_not_called()
        set_last_scan.assert_not_called()

    def _scan_two_message_batch_with_identity_failure(self, get_side_effect):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        messages = [
            {
                "id": "graph-earlier",
                "internetMessageId": "<earlier@example.test>",
                "conversationId": "conversation-1",
                "subject": "RE: 912 Gemini St",
                "from": {"emailAddress": {"address": "broker@example.test"}},
                "sender": {"emailAddress": {"address": "broker@example.test"}},
                "receivedDateTime": (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
                "bodyPreview": "The property has 600A power.",
                "hasAttachments": False,
            },
            {
                "id": "graph-newest",
                "internetMessageId": "<newest@example.test>",
                "conversationId": "conversation-1",
                "subject": "RE: 912 Gemini St",
                "from": {"emailAddress": {"address": "broker@example.test"}},
                "sender": {"emailAddress": {"address": "broker@example.test"}},
                "receivedDateTime": now.isoformat().replace("+00:00", "Z"),
                "bodyPreview": "It also has three-phase service.",
                "hasAttachments": False,
            },
        ]
        graph_responses = iter([
            _GraphResponse({"value": messages}),
            _GraphResponse({
                "body": {"contentType": "Text", "content": "The property has 600A power."},
                "hasAttachments": False,
            }),
        ])

        with patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=lambda _request: next(graph_responses),
        ), patch.object(processing.requests, "get", side_effect=get_side_effect) as graph_get, patch.object(
            processing,
            "has_processed",
            return_value=False,
        ), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(processing, "save_message", return_value=True), patch.object(
            processing,
            "index_message_id",
            return_value=True,
        ), patch.object(processing, "_fs"), patch.object(
            processing,
            "process_inbox_message",
        ) as process_newest, patch.object(
            processing,
            "mark_processed",
        ) as mark_processed, patch.object(processing, "set_last_scan_iso") as set_last_scan, patch(
            "email_automation.followup.cancel_followup_on_response",
        ) as record_authority:
            result = processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        return {
            "result": result,
            "graphGet": graph_get,
            "processNewest": process_newest,
            "markProcessed": mark_processed,
            "setLastScan": set_last_scan,
            "recordAuthority": record_authority,
        }

    def test_batch_identity_401_or_429_is_retryable_without_authority_markers(self):
        for status_code in (401, 429):
            with self.subTest(status_code=status_code):
                result = self._scan_two_message_batch_with_identity_failure([
                    _GraphIdentityResponse(status_code),
                    _GraphIdentityResponse(status_code),
                ])

                self.assertEqual("error", result["result"]["status"])
                self.assertTrue(result["result"]["retryable"])
                self.assertEqual(2, result["graphGet"].call_count)
                result["recordAuthority"].assert_not_called()
                result["markProcessed"].assert_not_called()
                result["processNewest"].assert_not_called()
                result["setLastScan"].assert_not_called()

    def test_batch_identity_network_failure_is_retryable_without_authority_markers(self):
        result = self._scan_two_message_batch_with_identity_failure([
            ConnectionError("/me network unavailable"),
            ConnectionError("SentItems network unavailable"),
        ])

        self.assertEqual("error", result["result"]["status"])
        self.assertTrue(result["result"]["retryable"])
        self.assertEqual(2, result["graphGet"].call_count)
        result["recordAuthority"].assert_not_called()
        result["markProcessed"].assert_not_called()
        result["processNewest"].assert_not_called()
        result["setLastScan"].assert_not_called()

    def test_batch_mailbox_identity_is_resolved_only_once_per_scan(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        messages = []
        full_body_responses = []
        for index in range(3):
            message_number = index + 1
            messages.append({
                "id": f"graph-{message_number}",
                "internetMessageId": f"<message-{message_number}@example.test>",
                "conversationId": "conversation-1",
                "subject": "RE: 912 Gemini St",
                "from": {"emailAddress": {"address": "broker@example.test"}},
                "sender": {"emailAddress": {"address": "broker@example.test"}},
                "receivedDateTime": (now + timedelta(seconds=index)).isoformat().replace("+00:00", "Z"),
                "bodyPreview": f"Broker detail {message_number}",
                "hasAttachments": False,
            })
            if index < 2:
                full_body_responses.append(_GraphResponse({
                    "body": {"contentType": "Text", "content": f"Broker detail {message_number}"},
                    "hasAttachments": False,
                }))
        graph_responses = iter([_GraphResponse({"value": messages}), *full_body_responses])

        with patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=lambda _request: next(graph_responses),
        ), patch.object(processing, "has_processed", return_value=False), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(processing, "save_message", return_value=True), patch.object(
            processing,
            "index_message_id",
            return_value=True,
        ), patch.object(
            processing,
            "_resolve_current_mailbox_email",
            return_value="operator@example.test",
        ) as resolve_identity, patch.object(processing, "_fs"), patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
            return_value=False,
        ), patch.object(
            processing,
            "process_inbox_message",
            return_value=None,
        ), patch.object(processing, "mark_processed"), patch.object(
            processing,
            "set_last_scan_iso",
        ), patch(
            "email_automation.followup.cancel_followup_on_response",
        ):
            processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=3,
            )

        resolve_identity.assert_called_once_with({"Authorization": "Bearer fake"})

    def test_earlier_content_reply_records_authority_before_newest_quote_only_is_processed(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        earlier = {
            "id": "graph-earlier",
            "internetMessageId": "<earlier@example.test>",
            "conversationId": "conversation-1",
            "subject": "RE: 912 Gemini St",
            "from": {"emailAddress": {"address": "broker@example.test"}},
            "receivedDateTime": (now - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
            "bodyPreview": "The property has 600A power.",
            "hasAttachments": False,
        }
        newest_quote_only = {
            "id": "graph-newest",
            "internetMessageId": "<newest@example.test>",
            "conversationId": "conversation-1",
            "subject": "RE: 912 Gemini St",
            "from": {"emailAddress": {"address": "broker@example.test"}},
            "receivedDateTime": now.isoformat().replace("+00:00", "Z"),
            "bodyPreview": "On Thu, Baylor wrote:\n> Can you confirm the power?",
            "hasAttachments": False,
        }
        graph_responses = iter([
            _GraphResponse({"value": [earlier, newest_quote_only]}),
            _GraphResponse({
                "body": {"contentType": "Text", "content": "The property has 600A power."},
                "hasAttachments": False,
            }),
        ])
        trace = []
        followup_state = {
            "followUpStatus": "waiting",
            "hasInboundReply": False,
        }

        def record_authority(_user_id, _thread_id):
            trace.append(("authority", "<earlier@example.test>"))
            followup_state.update({
                "followUpStatus": "paused",
                "hasInboundReply": True,
            })

        def record_processed(_user_id, message_id):
            trace.append(("processed", message_id))

        with patch.object(
            processing,
            "exponential_backoff_request",
            side_effect=lambda _request: next(graph_responses),
        ), patch.object(processing, "has_processed", return_value=False), patch.object(
            processing,
            "_match_message_to_thread",
            return_value="thread-1",
        ), patch.object(processing, "save_message", return_value=True), patch.object(
            processing,
            "index_message_id",
            return_value=True,
        ), patch.object(
            processing,
            "_resolve_current_mailbox_email",
            return_value="operator@example.test",
        ), patch.object(
            processing,
            "_skip_inbox_retry_after_manual_continuation",
            return_value=False,
        ), patch.object(processing, "_fs"), patch.object(
            processing,
            "process_inbox_message",
            return_value=None,
        ) as process_newest, patch.object(
            processing,
            "mark_processed",
            side_effect=record_processed,
        ), patch.object(processing, "set_last_scan_iso"), patch(
            "email_automation.followup.cancel_followup_on_response",
            side_effect=record_authority,
        ):
            processing.scan_inbox_against_index(
                "uid-1",
                {"Authorization": "Bearer fake"},
                only_unread=False,
                top=2,
            )

        process_newest.assert_called_once_with(
            "uid-1",
            {"Authorization": "Bearer fake"},
            newest_quote_only,
            authenticated_mailbox_email="operator@example.test",
        )
        self.assertIn(("authority", "<earlier@example.test>"), trace)
        self.assertLess(
            trace.index(("authority", "<earlier@example.test>")),
            trace.index(("processed", "<earlier@example.test>")),
        )
        self.assertTrue(followup_state["hasInboundReply"])
        self.assertEqual("paused", followup_state["followUpStatus"])


if __name__ == "__main__":
    unittest.main()
