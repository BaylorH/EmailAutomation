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


class BatchedInboundAuthorityTests(unittest.TestCase):
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
