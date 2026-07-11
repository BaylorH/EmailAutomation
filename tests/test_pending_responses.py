import os
import sys
import types
import unittest
from contextvars import copy_context
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import pending_responses, processing
from email_automation.campaign_safety import CampaignAutomationDecision


class FakeDocRef:
    def __init__(self, doc=None, doc_id=None):
        self._doc = doc
        self.id = doc_id or getattr(doc, "id", None)
        self.deleted = False
        self.update_calls = []
        self.set_calls = []

    def delete(self):
        self.deleted = True

    def update(self, data):
        self.update_calls.append(data)

    def get(self):
        if self._doc is not None:
            return self._doc
        return types.SimpleNamespace(exists=False, to_dict=lambda: {})

    def set(self, data):
        self.set_calls.append(data)


class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = True
        self.reference = FakeDocRef(self, doc_id)

    def to_dict(self):
        return self._data


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.add_calls = []

    def stream(self):
        return list(self.docs)

    def add(self, data):
        self.add_calls.append(data)
        return FakeDocRef()

    def document(self, doc_id):
        for doc in self.docs:
            if doc.id == doc_id:
                return doc.reference
        return FakeDocRef(doc_id=doc_id)


class FakeFirestore:
    def __init__(self, pending_docs):
        self.collections = {
            "pendingResponses": FakeCollection(pending_docs),
            "deadLetterQueue": FakeCollection(),
        }

    def collection(self, name):
        return self

    def document(self, name):
        return self

    def collection(self, name):
        return self.collections.setdefault(name, FakeCollection()) if name != "users" else self


class PendingResponsesTests(unittest.TestCase):
    def test_failed_send_without_local_outcome_keeps_current_retry(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([active_doc])
        def fake_send_reply_in_thread(**_kwargs):
            return False

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses, "find_matching_sent_message_for_retry", return_value=None
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertFalse(active_doc.reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls)
        self.assertEqual(1, active_doc.reference.update_calls[-1]["attempts"])
        self.assertEqual("send_reply_in_thread returned False", states[0]["error"])

    def test_reply_campaign_suppression_outcome_is_isolated_per_execution_context(self):
        terminal = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_stopped",
            client_data={},
            metadata={"terminal": True},
        )
        maintenance = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={},
            metadata={"terminal": False},
        )
        terminal_context = copy_context()
        maintenance_context = copy_context()

        terminal_context.run(processing._set_reply_campaign_suppression, terminal)
        maintenance_context.run(processing._set_reply_campaign_suppression, maintenance)

        self.assertEqual(
            "terminal",
            terminal_context.run(processing._get_reply_campaign_suppression)[0],
        )
        self.assertEqual(
            "maintenance",
            maintenance_context.run(processing._get_reply_campaign_suppression)[0],
        )

    def test_pending_suppression_ignores_stale_shared_send_attributes(self):
        token = processing._REPLY_SEND_OUTCOME.set(processing.ReplySendOutcome())
        self.addCleanup(processing._REPLY_SEND_OUTCOME.reset, token)
        stale_decision = CampaignAutomationDecision(
            state="blocked",
            reason="other_campaign_stopped",
            client_data={},
            metadata={"terminal": True},
        )

        with patch.object(
            processing.send_reply_in_thread,
            "last_outcome",
            "blocked_campaign_terminal",
            create=True,
        ), patch.object(
            processing.send_reply_in_thread,
            "last_campaign_decision",
            stale_decision,
            create=True,
        ):
            kind, decision = pending_responses._get_local_campaign_suppression()

        self.assertIsNone(kind)
        self.assertIsNone(decision)

    def setUp(self):
        self._campaign_decision_patch = patch.object(
            pending_responses,
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

    def _mock_clients_module(self, fake_fs):
        return patch.dict(
            sys.modules,
            {"email_automation.clients": types.SimpleNamespace(_fs=fake_fs)},
        )

    def test_max_attempt_pending_response_moves_to_dead_letter_queue(self):
        stale_doc = FakeDoc("thread-stale", {
            "threadId": "thread-stale",
            "msgId": "message-1",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nThanks",
            "clientId": "client-1",
            "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
            "lastError": "Graph failed repeatedly",
        })
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([stale_doc, active_doc])

        with self._mock_clients_module(fake_fs):
            valid = pending_responses.get_pending_responses("uid-1")

        self.assertEqual([item["doc"].id for item in valid], ["thread-active"])
        self.assertTrue(stale_doc.reference.deleted)
        dead_letter = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["originalDocId"], "thread-stale")
        self.assertEqual(dead_letter["threadId"], "thread-stale")
        self.assertEqual(dead_letter["recipient"], "bp21harrison@gmail.com")
        self.assertEqual(dead_letter["failureReason"], "Graph failed repeatedly")

    def test_failed_retry_preserves_detailed_send_error_when_available(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            processing._set_reply_send_outcome(
                error="HTTP 429 rate limited after 3 attempts",
                outcome="send_failed",
            )
            return False

        fake_send_reply_in_thread.last_error = "HTTP 429 rate limited after 3 attempts"

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(pending_responses, "find_matching_sent_message_for_retry", return_value=None):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # A swallowed per-item Graph send failure must surface exactly one
        # "error" op-state to the health rail (not merely "no healthy state").
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], "error")
        self.assertEqual(sent[0]["operation"], "pending_response_send")
        self.assertEqual(sent[0]["recipient"], "bp21harrison@gmail.com")
        self.assertEqual(sent[0]["error"], "HTTP 429 rate limited after 3 attempts")
        retry_payload = active_doc.reference.update_calls[-1]
        self.assertEqual(retry_payload["attempts"], 2)
        self.assertEqual(retry_payload["lastError"], "HTTP 429 rate limited after 3 attempts")

    def test_unsafe_pending_response_moves_to_dead_letter_without_sending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi [NAME],\n\nCan you confirm the availability?",
            "clientId": "client-1",
            "attempts": 0,
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("unsafe pending response should stop before Graph send")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        self.assertTrue(active_doc.reference.deleted)
        self.assertEqual([], active_doc.reference.update_calls)
        dead_letter = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["originalDocId"], "thread-active")
        self.assertIn("Unresolved outbound placeholder", dead_letter["failureReason"])
        self.assertIn("manual review", dead_letter["failureReason"])

    def test_maintenance_pause_preserves_pending_response_without_sending(self):
        active_doc = FakeDoc("thread-maintenance", {
            "threadId": "thread-maintenance",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 2,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([active_doc])
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="blocked",
            reason="campaign_maintenance",
            client_data={"status": "live", "automationPaused": True},
            metadata={"terminal": False, "stopKind": "maintenance_pause"},
        )

        def fail_send(**_kwargs):
            raise AssertionError("maintenance-paused pending response must not send")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fail_send
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        self.assertFalse(active_doc.reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls)
        payload = active_doc.reference.update_calls[-1]
        self.assertEqual("queued", payload["status"])
        self.assertEqual("blocked", payload["automationSuppressedState"])
        self.assertEqual(2, active_doc.to_dict()["attempts"])

    def test_unknown_campaign_state_preserves_pending_response_without_sending(self):
        active_doc = FakeDoc("thread-unknown", {
            "threadId": "thread-unknown",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": pending_responses.MAX_RESPONSE_ATTEMPTS,
        })
        fake_fs = FakeFirestore([active_doc])
        self.campaign_decision.return_value = CampaignAutomationDecision(
            state="unknown",
            reason="client_automation_state_read_error",
            client_data={},
            metadata={"terminal": False, "stopKind": "none"},
        )

        def fail_send(**_kwargs):
            raise AssertionError("unknown campaign state must not send")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fail_send
        ):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual([], states)
        self.assertFalse(active_doc.reference.deleted)
        self.assertEqual([], fake_fs.collections["deadLetterQueue"].add_calls)
        payload = active_doc.reference.update_calls[-1]
        self.assertEqual("unknown", payload["automationSuppressedState"])
        self.assertNotIn("attempts", payload)

    def test_sent_but_unindexed_retry_moves_to_reconciliation_without_resending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Temporary failure",
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            processing._set_reply_send_outcome(
                error="Graph accepted reply but Sent Items lookup failed",
                sent_but_unindexed=True,
                outcome="sent_but_unindexed",
            )
            return False

        fake_send_reply_in_thread.last_error = "Graph accepted reply but Sent Items lookup failed"
        fake_send_reply_in_thread.sent_but_unindexed = True

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(pending_responses, "find_matching_sent_message_for_retry", return_value=None):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        self.assertEqual([], active_doc.reference.update_calls)
        self.assertTrue(active_doc.reference.deleted)
        dead_letter = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["status"], "needs_reconciliation")
        self.assertTrue(dead_letter["alreadySent"])
        self.assertEqual(dead_letter["failureReason"], "Graph accepted reply but Sent Items lookup failed")

    def test_retry_with_matching_sent_item_moves_to_reconciliation_without_resending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "subject": "0 Gemini Ave, Houston",
            "conversationId": "conv-1",
        })
        fake_fs = FakeFirestore([active_doc])
        sent_match = {
            "id": "sent-reply-1",
            "internetMessageId": "<sent-reply-1@example.com>",
            "conversationId": "conversation-1",
            "subject": "RE: 0 Gemini Ave",
        }

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("retry guard should stop before resending")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses,
            "find_matching_sent_message_for_retry",
            return_value=sent_match,
            create=True,
        ) as sent_guard:
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        sent_guard.assert_called_once()
        self.assertEqual(sent_guard.call_args.kwargs["subject"], "0 Gemini Ave, Houston")
        self.assertEqual(sent_guard.call_args.kwargs["conversation_id"], "conv-1")
        self.assertEqual([], active_doc.reference.update_calls)
        self.assertTrue(active_doc.reference.deleted)
        dead_letter = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertEqual(dead_letter["status"], "needs_reconciliation")
        self.assertTrue(dead_letter["alreadySent"])
        self.assertEqual(dead_letter["sentMessageId"], "sent-reply-1")
        self.assertEqual(dead_letter["internetMessageId"], "<sent-reply-1@example.com>")

    def test_retry_blocks_when_conversation_was_manually_continued(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
            "subject": "0 Gemini Ave, Houston",
            "conversationId": "conv-1",
        })
        fake_fs = FakeFirestore([active_doc])
        manual_continuation = {
            "id": "manual-sent-1",
            "internetMessageId": "<manual-sent-1@example.com>",
            "conversationId": "conv-1",
            "sentDateTime": "2026-06-26T12:04:00Z",
        }

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("manual continuation guard should stop before resending")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses,
            "find_matching_sent_message_for_retry",
            return_value=None,
            create=True,
        ), patch.object(
            pending_responses,
            "find_sent_conversation_continuation_for_retry",
            return_value=manual_continuation,
            create=True,
        ) as continuation_guard:
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        continuation_guard.assert_called_once()
        self.assertEqual(continuation_guard.call_args.kwargs["conversation_id"], "conv-1")
        self.assertTrue(active_doc.reference.deleted)
        self.assertEqual([], active_doc.reference.update_calls)
        dead_letter = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertIn("manually continued", dead_letter["failureReason"])

    def test_retry_guard_lookup_failure_dead_letters_without_resending(self):
        active_doc = FakeDoc("thread-active", {
            "threadId": "thread-active",
            "msgId": "message-2",
            "recipient": "bp21harrison@gmail.com",
            "responseBody": "Hi,\n\nCan you share the flyer?",
            "clientId": "client-1",
            "attempts": 1,
            "lastError": "Read timed out after Graph reply",
            "lastSendAttemptAt": "2026-06-26T12:00:00Z",
        })
        fake_fs = FakeFirestore([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            raise AssertionError("retry guard should stop before resending")

        with self._mock_clients_module(fake_fs), patch.object(
            processing, "send_reply_in_thread", new=fake_send_reply_in_thread
        ), patch.object(
            pending_responses,
            "find_matching_sent_message_for_retry",
            side_effect=pending_responses.SentMailGuardLookupError("Graph 401"),
        ):
            sent = pending_responses.process_pending_responses("uid-1", {"Authorization": "Bearer token"})

        # Handled outcome (dead-letter / reconciliation): no send was attempted,
        # so no op-state escalates the health rail. Assert the exact empty shape.
        self.assertEqual(sent, [])
        self.assertTrue(active_doc.reference.deleted)
        self.assertEqual([], active_doc.reference.update_calls)
        dead_letter = fake_fs.collections["deadLetterQueue"].add_calls[-1]
        self.assertEqual(dead_letter["source"], "pendingResponses")
        self.assertIn("Sent Items retry guard could not verify prior send", dead_letter["failureReason"])


if __name__ == "__main__":
    unittest.main()
