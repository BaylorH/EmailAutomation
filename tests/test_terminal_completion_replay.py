import copy
import hashlib
import json
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from email_automation import processing


class _Snapshot:
    def __init__(self, data=None, *, exists=True):
        self._data = copy.deepcopy(data or {})
        self.exists = exists

    def to_dict(self):
        return copy.deepcopy(self._data)


class _Query:
    def __init__(self, docs, filters=None):
        self._docs = list(docs)
        self._filters = list(filters or [])

    def where(self, *, filter):
        return _Query(self._docs, [*self._filters, filter])

    def stream(self):
        docs = list(self._docs)
        for field_filter in self._filters:
            docs = [
                doc
                for doc in docs
                if doc.to_dict().get(field_filter.field_path)
                == field_filter.value
            ]
        return docs


class _Collection(_Query):
    def __init__(self, docs=None):
        self._docs_by_id = dict(docs or {})
        super().__init__(self._docs_by_id.values())

    def document(self, doc_id):
        return self._docs_by_id[doc_id]


class _Doc:
    def __init__(
        self,
        doc_id,
        data=None,
        *,
        collections=None,
        set_failures=0,
    ):
        self.id = doc_id
        self.reference = self
        self._data = copy.deepcopy(data or {})
        self._collections = dict(collections or {})
        self._set_failures = set_failures
        self.set_attempts = 0
        self.set_calls = []
        self.update_calls = []

    def to_dict(self):
        return copy.deepcopy(self._data)

    def get(self, transaction=None):
        return _Snapshot(self._data)

    def set(self, payload, merge=False):
        self.set_attempts += 1
        if self._set_failures:
            self._set_failures -= 1
            raise RuntimeError("transient local completion write failure")
        self.set_calls.append((copy.deepcopy(payload), merge))
        if merge:
            self._data.update(copy.deepcopy(payload))
        else:
            self._data = copy.deepcopy(payload)

    def update(self, payload):
        self.update_calls.append(copy.deepcopy(payload))
        self._data.update(copy.deepcopy(payload))

    def collection(self, name):
        return self._collections.setdefault(name, _Collection())


class _Firestore:
    def __init__(self, users):
        self._collections = {"users": _Collection(users)}

    def collection(self, name):
        return self._collections[name]


class TerminalCompletionReplayTests(unittest.TestCase):
    USER_ID = "uid-1"
    CLIENT_ID = "client-1"
    THREAD_ID = "thread-1"
    SOURCE_GRAPH_ID = "graph-terminal-source-1"
    SOURCE_INTERNET_ID = "<terminal-source-1@mock.test>"

    def _settlement(self, *, complete_client_after_reply):
        saga_immutable = {
            "version": processing.TERMINAL_SAGA_VERSION,
            "settlementOrdinal": 1,
            "sagaKey": "terminal-saga-1",
            "sourceMessageKey": self.SOURCE_INTERNET_ID,
            "sourceGraphMessageId": self.SOURCE_GRAPH_ID,
            "sourceInternetMessageId": self.SOURCE_INTERNET_ID,
            "sourceConversationId": "conversation-terminal-1",
            "sourceRow": 3,
            "rowAnchor": "951 E FM 646",
            "note": "Property is no longer available.",
            "clientId": self.CLIENT_ID,
            "replyRecipient": "broker@example.test",
            "responseScenario": "none",
            "responseBody": None,
            "notificationRequired": False,
            "completeClientAfterReply": complete_client_after_reply,
            "finalizationPlan": {
                "dividerRow": 10,
                "finalRow": 10,
                "claimThreadId": self.THREAD_ID,
                "terminalThreadIds": [self.THREAD_ID],
                "rowShifts": [],
                "writeCount": 1,
            },
        }
        saga_hash = hashlib.sha256(
            json.dumps(
                saga_immutable,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        saga_snapshot = {
            **saga_immutable,
            "immutableHash": saga_hash,
            "phase": "finalized",
            "finalRow": 10,
        }
        now = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
        sheet_attempt_immutable = {
            "version": processing.TERMINAL_SHEET_MUTATION_VERSION,
            "sagaKey": saga_snapshot["sagaKey"],
            "sagaImmutableHash": saga_hash,
            "attemptId": "sheet-attempt-terminal-saga-1-1",
            "ordinal": 1,
            "previousAttemptId": None,
            "previousAttemptHash": None,
            "mutationKind": "move_with_note",
            "sourceRow": 3,
            "finalRow": 10,
            "rowAnchor": saga_snapshot["rowAnchor"],
            "noteHash": hashlib.sha256(
                saga_snapshot["note"].encode("utf-8")
            ).hexdigest(),
            "owner": "terminal-owner-1",
            "fencingToken": 1,
            "requestStartedAt": now,
            "providerDeadline": now + timedelta(seconds=60),
        }
        sheet_attempt = {
            **sheet_attempt_immutable,
            "attemptImmutableHash": hashlib.sha256(
                json.dumps(
                    sheet_attempt_immutable,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
            "status": "applied",
            "appliedByOwner": "terminal-owner-1",
            "appliedByFencingToken": 1,
            "providerCompletedAt": now + timedelta(seconds=1),
            "operatorReviewRequired": False,
        }
        sheet_attempt["attemptHash"] = hashlib.sha256(
            json.dumps(
                sheet_attempt,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        projection_immutable = {
            "version": processing.TERMINAL_SETTLEMENT_VERSION,
            "settlementOrdinal": 1,
            "sagaKey": saga_snapshot["sagaKey"],
            "sagaImmutableHash": saga_hash,
            "sourceMessageKey": self.SOURCE_INTERNET_ID,
            "sourceGraphMessageId": self.SOURCE_GRAPH_ID,
            "sourceInternetMessageId": self.SOURCE_INTERNET_ID,
            "finalRow": 10,
            "notificationOutcome": "not_required",
            "replyOutcome": "not_required",
            "sagaSnapshot": saga_snapshot,
            "terminalReplyAttempt": None,
            "terminalReplyAttemptHash": None,
            "sheetMutationAttempt": sheet_attempt,
            "sheetMutationHistory": [],
            "sheetMutationReview": None,
            "settledAt": now + timedelta(seconds=2),
        }
        projection = {
            **projection_immutable,
            "projectionHash": hashlib.sha256(
                json.dumps(
                    projection_immutable,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest(),
        }
        return processing._validate_terminal_settlement_projection(projection)

    def _firestore_fixture(self, settlement, *, client_status, set_failures=0):
        thread_doc = _Doc(self.THREAD_ID, {
            "clientId": self.CLIENT_ID,
            "status": processing.THREAD_STATUS["stopped"],
            "terminalSaga": None,
            "terminalSagaKey": None,
            "terminalNotificationOwed": False,
            "terminalReplyOwed": False,
            "terminalReplyAttempt": None,
            "terminalSettlements": [copy.deepcopy(settlement)],
        })
        client_doc = _Doc(
            self.CLIENT_ID,
            {"status": client_status},
            collections={"notifications": _Collection()},
            set_failures=set_failures,
        )
        user_doc = _Doc(self.USER_ID, collections={
            "threads": _Collection({self.THREAD_ID: thread_doc}),
            "clients": _Collection({self.CLIENT_ID: client_doc}),
            "outbox": _Collection(),
            "pendingResponses": _Collection(),
            "deadLetterQueue": _Collection(),
            "terminalGraphSendReviews": _Collection(),
            "graphSendDraftReviews": _Collection(),
        })
        return _Firestore({self.USER_ID: user_doc}), thread_doc, client_doc

    def _message(self):
        return {
            "id": self.SOURCE_GRAPH_ID,
            "subject": "RE: terminal property update",
            "from": {
                "emailAddress": {
                    "address": "broker@example.test",
                    "name": "Broker",
                },
            },
            "toRecipients": [{
                "emailAddress": {"address": "operator@example.test"},
            }],
            "internetMessageId": self.SOURCE_INTERNET_ID,
            "conversationId": "conversation-terminal-1",
            "receivedDateTime": "2026-08-02T10:00:00Z",
            "bodyPreview": "The property is no longer available.",
            "hasAttachments": False,
            "internetMessageHeaders": [{
                "name": "In-Reply-To",
                "value": "<tracked-outbound-1@mock.test>",
            }],
        }

    def _effect_mocks(self):
        return {
            name: MagicMock(name=name)
            for name in (
                "_resume_exact_terminal_saga",
                "_sheets_client",
                "send_reply_in_thread",
                "queue_pending_response",
                "_stage_terminal_saga",
                "_settle_terminal_reply_obligation",
                "_clear_resolved_terminal_saga",
                "move_row_below_divider",
                "move_row_below_new_divider_atomic",
                "propose_sheet_updates",
                "apply_proposal_to_sheet",
                "save_message",
                "_persist_inbound_message_history",
            )
        }

    def _process_exact_source(self, firestore, effects):
        full_body = MagicMock()
        full_body.json.return_value = {
            "body": {
                "content": "The property is no longer available.",
                "contentType": "Text",
            },
            "hasAttachments": False,
        }
        me_response = MagicMock(status_code=200)
        me_response.json.return_value = {"mail": "operator@example.test"}

        with ExitStack() as stack:
            stack.enter_context(patch.object(processing, "_fs", firestore))
            stack.enter_context(patch.object(
                processing,
                "exponential_backoff_request",
                return_value=full_body,
            ))
            stack.enter_context(patch.object(
                processing.requests,
                "get",
                return_value=me_response,
            ))
            stack.enter_context(patch.object(
                processing,
                "lookup_thread_by_message_id",
                return_value=self.THREAD_ID,
            ))
            stack.enter_context(patch.object(
                processing,
                "lookup_thread_by_conversation_id",
                return_value=None,
            ))
            for name, effect_mock in effects.items():
                stack.enter_context(patch.object(
                    processing,
                    name,
                    new=effect_mock,
                ))
            return processing.process_inbox_message(
                self.USER_ID,
                {"Authorization": "Bearer test-token"},
                self._message(),
            )

    def _assert_no_terminal_effect_replay(self, effects):
        for name, effect_mock in effects.items():
            with self.subTest(effect=name):
                effect_mock.assert_not_called()

    def test_exact_settlement_replays_completion_after_transient_post_cleanup_failure(self):
        settlement = self._settlement(complete_client_after_reply=True)
        firestore, thread_doc, client_doc = self._firestore_fixture(
            settlement,
            client_status="live",
            set_failures=1,
        )
        effects = self._effect_mocks()

        with self.assertRaises(processing.RetryableProcessingError):
            self._process_exact_source(firestore, effects)

        self.assertEqual("live", client_doc._data["status"])
        self.assertEqual(1, client_doc.set_attempts)
        self._assert_no_terminal_effect_replay(effects)

        self._process_exact_source(firestore, effects)

        self.assertEqual("completed", client_doc._data["status"])
        self.assertEqual(2, client_doc.set_attempts)
        self.assertEqual(
            [settlement],
            thread_doc._data["terminalSettlements"],
        )
        self.assertIsNone(thread_doc._data.get("terminalSaga"))
        self.assertIsNone(thread_doc._data.get("terminalSagaKey"))
        self._assert_no_terminal_effect_replay(effects)

    def test_exact_settlement_without_completion_obligation_remains_done(self):
        settlement = self._settlement(complete_client_after_reply=False)
        firestore, thread_doc, client_doc = self._firestore_fixture(
            settlement,
            client_status="live",
        )
        effects = self._effect_mocks()

        self._process_exact_source(firestore, effects)

        self.assertEqual("live", client_doc._data["status"])
        self.assertEqual(0, client_doc.set_attempts)
        self.assertEqual(
            [settlement],
            thread_doc._data["terminalSettlements"],
        )
        self._assert_no_terminal_effect_replay(effects)

    def test_exact_settlement_completion_obligation_is_noop_when_client_already_completed(self):
        settlement = self._settlement(complete_client_after_reply=True)
        firestore, thread_doc, client_doc = self._firestore_fixture(
            settlement,
            client_status="completed",
        )
        effects = self._effect_mocks()

        self._process_exact_source(firestore, effects)

        self.assertEqual("completed", client_doc._data["status"])
        self.assertEqual(0, client_doc.set_attempts)
        self.assertEqual(
            [settlement],
            thread_doc._data["terminalSettlements"],
        )
        self._assert_no_terminal_effect_replay(effects)


if __name__ == "__main__":
    unittest.main()
