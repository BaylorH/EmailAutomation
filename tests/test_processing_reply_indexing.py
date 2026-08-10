import os
import unittest
from contextlib import ExitStack
from contextvars import copy_context
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault("SITESIFT_AUTO_REPLY_ALLOWLIST", "*")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import email as email_module
from email_automation import pending_responses, processing, sheet_operations
from email_automation.campaign_safety import CampaignAutomationDecision


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"Unexpected HTTP status {self.status_code}")


class FakeSnapshot:
    def __init__(self, data=None, exists=True, doc_id=None, reference=None):
        self._data = data or {}
        self.exists = exists
        self.id = doc_id
        self.reference = reference

    def to_dict(self):
        return self._data


class FakeFirestore:
    def __init__(self, path=()):
        self.path = path

    def collection(self, name):
        return FakeFirestore(self.path + (name,))

    def document(self, doc_id):
        return FakeFirestore(self.path + (doc_id,))

    def get(self):
        if self.path == ("systemConfig", "campaignAccess"):
            return FakeSnapshot({"automationEnabled": True, "allowedUids": []})
        if len(self.path) == 2 and self.path[0] == "users":
            return FakeSnapshot({"email": "baylor.freelance@outlook.com"})
        if len(self.path) == 4 and self.path[2] == "threads":
            return FakeSnapshot({"clientId": "client-1", "status": "active"})
        if len(self.path) == 4 and self.path[2] == "clients":
            return FakeSnapshot({"status": "live", "automationPaused": False})
        return FakeSnapshot(exists=False)


class SendCounterFirestore:
    """Path-addressed Firestore double that applies real Increment transforms."""

    def __init__(self, store=None, path=(), transaction_state=None):
        self.store = store if store is not None else {}
        self.path = path
        self.transaction_state = transaction_state or {"fail_commits": 0}

    def collection(self, name):
        return SendCounterFirestore(
            self.store, self.path + (name,), self.transaction_state,
        )

    def document(self, doc_id):
        return SendCounterFirestore(
            self.store, self.path + (doc_id,), self.transaction_state,
        )

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        data = self.store.get(self.path)
        return FakeSnapshot(data, exists=data is not None)

    def set(self, data, merge=False):
        current = dict(self.store.get(self.path) or {}) if merge else {}
        for key, value in data.items():
            if key == "count" and hasattr(value, "value"):
                current[key] = int(current.get(key) or 0) + int(value.value)
            else:
                current[key] = value
        self.store[self.path] = current

    def update(self, data):
        self.set(data, merge=True)

    def delete(self):
        self.store.pop(self.path, None)

    def where(self, *_args, **_kwargs):
        return self

    def stream(self):
        depth = len(self.path) + 1
        return [
            FakeSnapshot(data, doc_id=path[-1], reference=SendCounterFirestore(
                self.store, path, self.transaction_state,
            ))
            for path, data in self.store.items()
            if len(path) == depth and path[:len(self.path)] == self.path
        ]

    def count(self, *path):
        return int((self.store.get(tuple(path)) or {}).get("count") or 0)

    def transaction(self):
        return SendCounterTransaction(self)

    def fail_next_transaction(self):
        self.transaction_state["fail_commits"] += 1


class SendCounterTransaction:
    def __init__(self, fs):
        self.fs = fs
        self.writes = []

    def get(self, ref):
        data = self.fs.store.get(ref.path)
        return FakeSnapshot(data, exists=data is not None)

    def set(self, ref, data, merge=False):
        self.writes.append((ref.path, dict(data), merge))

    def commit(self):
        if self.fs.transaction_state["fail_commits"]:
            self.fs.transaction_state["fail_commits"] -= 1
            raise RuntimeError("atomic counter transaction failed")
        staged = {path: dict(data) for path, data in self.fs.store.items()}
        for path, data, merge in self.writes:
            current = dict(staged.get(path) or {}) if merge else {}
            for key, value in data.items():
                if key == "count" and hasattr(value, "value"):
                    current[key] = int(current.get(key) or 0) + int(value.value)
                else:
                    current[key] = value
            staged[path] = current
        self.fs.store.clear()
        self.fs.store.update(staged)


def fake_transactional(callback):
    def wrapped(transaction, *args, **kwargs):
        result = callback(transaction, *args, **kwargs)
        transaction.commit()
        return result

    return wrapped


class ProcessingReplyIndexingTests(unittest.TestCase):
    def setUp(self):
        cap_env = patch.dict(os.environ, {
            "SITESIFT_DAILY_SEND_CAP": "0", "SITESIFT_GLOBAL_DAILY_SEND_CAP": "0",
        })
        cap_env.start()
        self.addCleanup(cap_env.stop)

    def test_other_context_terminal_outcome_cannot_suppress_current_retry(self):
        terminal = CampaignAutomationDecision(
            state="blocked",
            reason="other_campaign_stopped",
            client_data={},
            metadata={"terminal": True},
        )
        self.addCleanup(processing._reset_reply_send_outcome)
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="current request failed",
            outcome="send_failed",
            sent_but_unindexed=False,
        )
        current_context = copy_context()
        other_context = copy_context()
        other_context.run(processing._set_reply_campaign_suppression, terminal)

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as reconcile:
            outcome = current_context.run(
                processing._queue_response_retry_or_reconciliation,
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
            )

        self.assertEqual("queued_retry", outcome)
        queue_retry.assert_called_once()
        reconcile.assert_not_called()

    @patch.object(processing.time, "sleep", return_value=None)
    @patch.object(processing.requests, "get")
    def test_sent_reply_lookup_skips_older_conversation_messages(self, requests_get, _sleep):
        requests_get.return_value = FakeResponse(200, {
            "value": [
                {
                    "id": "original-outreach",
                    "internetMessageId": "<original@example.com>",
                    "conversationId": "conversation-1",
                    "sentDateTime": "2026-06-09T18:53:28Z",
                    "bodyPreview": "Original outreach",
                },
                {
                    "id": "closing-reply",
                    "internetMessageId": "<closing@example.com>",
                    "conversationId": "conversation-1",
                    "sentDateTime": "2026-06-09T19:09:27Z",
                    "bodyPreview": "Perfect, thank you",
                },
            ]
        })

        sent = processing._find_recent_sent_message_for_conversation(
            {"Authorization": "Bearer token"},
            "https://graph.microsoft.com/v1.0",
            "conversation-1",
            datetime(2026, 6, 9, 19, 9, 0, tzinfo=timezone.utc),
            attempts=1,
        )

        self.assertEqual("closing-reply", sent["id"])
        self.assertEqual("<closing@example.com>", sent["internetMessageId"])
        self.assertIn("sentDateTime ge 2026-06-09T19:09:00Z", requests_get.call_args.kwargs["params"]["$filter"])

    def test_sent_but_unindexed_auto_response_is_not_queued_for_retry(self):
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="Failed to index reply after 3 attempts",
            sent_but_unindexed=True,
            outcome="sent_but_unindexed",
        )

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as record_reconciliation:
            outcome = processing._queue_response_retry_or_reconciliation(
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
                source_context="autoResponse",
            )

        self.assertEqual("sent_unindexed", outcome)
        queue_retry.assert_not_called()
        record_reconciliation.assert_called_once_with(
            "uid-1",
            "thread-1",
            "msg-1",
            "bp21harrison@gmail.com",
            "Hi,\n\nThanks.",
            "client-1",
            "Failed to index reply after 3 attempts",
            source_context="autoResponse",
        )

    def test_sent_but_unindexed_outcome_counts_as_response_attempted(self):
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="Failed to index reply after 3 attempts",
            sent_but_unindexed=True,
            outcome="sent_but_unindexed",
        )

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as record_reconciliation:
            attempted = processing._handle_auto_response_send_failure(
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
                failure_label="thank you email",
            )

        self.assertTrue(attempted)
        queue_retry.assert_not_called()
        record_reconciliation.assert_called_once()

    def test_opted_out_recipient_suppression_is_not_queued_for_retry(self):
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="All reply-all recipients opted out",
            outcome="suppressed_recipient_optout",
        )

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as reconcile:
            outcome = processing._queue_response_retry_or_reconciliation(
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
            )

        self.assertEqual("recipient_suppressed", outcome)
        queue_retry.assert_not_called()
        reconcile.assert_not_called()

    def test_opted_out_recipient_suppression_counts_as_handled(self):
        processing._reset_reply_send_outcome()
        processing._set_reply_send_outcome(
            error="All reply-all recipients opted out",
            outcome="suppressed_recipient_optout",
        )

        with patch.object(processing, "queue_pending_response") as queue_retry, \
                patch.object(processing, "record_sent_unindexed_response") as reconcile:
            handled = processing._handle_auto_response_send_failure(
                "uid-1",
                "thread-1",
                "msg-1",
                "bp21harrison@gmail.com",
                "Hi,\n\nThanks.",
                "client-1",
            )

        self.assertTrue(handled)
        queue_retry.assert_not_called()
        reconcile.assert_not_called()

    def test_auto_thread_reply_preserves_safe_cc_with_reply_all_draft(self):
        posts = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                })
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [
                {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
            ],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", return_value=None):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertFalse(any(url.endswith("/reply") for url in posts))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["baylor@manifoldengineering.ai"],
        )
        self.assertEqual(saved_messages[0][3]["cc"], ["baylor@manifoldengineering.ai"])

    def test_auto_thread_reply_fetches_created_draft_when_graph_omits_recipients(self):
        posts = []
        gets = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "auto-reply-draft-1"})
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                })
            if url.endswith("/me/messages/auto-reply-draft-1"):
                return FakeResponse(200, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [
                        {"emailAddress": {"address": "bp21harrison@gmail.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [
                {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
            ],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", return_value=None):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/me/messages/auto-reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["ccRecipients"]],
            ["baylor@manifoldengineering.ai"],
        )
        self.assertEqual(saved_messages[0][3]["cc"], ["baylor@manifoldengineering.ai"])

    def test_auto_thread_reply_rebuilds_reply_all_from_source_when_draft_stays_empty(self):
        posts = []
        gets = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "auto-reply-draft-1"})
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                    "from": {
                        "emailAddress": {
                            "name": "Avery",
                            "address": "bp21harrison@gmail.com",
                        }
                    },
                    "toRecipients": [
                        {"emailAddress": {"address": "baylor.freelance@outlook.com"}},
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": "baylor@manifoldengineering.ai"}},
                    ],
                })
            if url.endswith("/me/messages/auto-reply-draft-1"):
                return FakeResponse(200, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [],
                    "ccRecipients": [],
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        def fake_optout(_user_id, email):
            if email.lower() == "baylor@manifoldengineering.ai":
                return {"reason": "temporary proof opt-out"}
            return None

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", side_effect=fake_optout):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/me/messages/msg-1") for url in gets))
        self.assertTrue(any(url.endswith("/me/messages/auto-reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(patch_payload["ccRecipients"], [])
        self.assertEqual(saved_messages[0][3]["cc"], [])

    def test_auto_thread_reply_falls_back_to_current_recipient_when_graph_stays_empty(self):
        posts = []
        gets = []
        patch_payloads = []
        saved_messages = []

        def fake_retry(func, **_kwargs):
            return func()

        def fake_post(url, **_kwargs):
            posts.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {"id": "auto-reply-draft-1"})
            if url.endswith("/auto-reply-draft-1/send"):
                return FakeResponse(202)
            return FakeResponse(500)

        def fake_get(url, **_kwargs):
            gets.append(url)
            if url.endswith("/me/messages/msg-1"):
                return FakeResponse(200, {
                    "conversationId": "conv-1",
                    "subject": "RE: 101 Launch Complete Way",
                })
            if url.endswith("/me/messages/auto-reply-draft-1"):
                return FakeResponse(200, {
                    "id": "auto-reply-draft-1",
                    "toRecipients": [],
                    "ccRecipients": [],
                })
            return FakeResponse(404)

        def fake_patch(_url, **kwargs):
            patch_payloads.append(kwargs.get("json") or {})
            return FakeResponse(200)

        sent_message = {
            "id": "sent-1",
            "internetMessageId": "<sent-1@example.com>",
            "conversationId": "conv-1",
            "subject": "RE: 101 Launch Complete Way",
            "sentDateTime": "2026-06-28T16:00:00Z",
            "toRecipients": [
                {"emailAddress": {"address": "bp21harrison@gmail.com"}},
            ],
            "ccRecipients": [],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }

        with patch("email_automation.utils.exponential_backoff_request", side_effect=fake_retry), \
                patch("email_automation.clients._fs", FakeFirestore()), \
                patch.object(processing.requests, "get", side_effect=fake_get), \
                patch.object(processing.requests, "post", side_effect=fake_post), \
                patch.object(processing.requests, "patch", side_effect=fake_patch), \
                patch.object(processing.time, "sleep", return_value=None), \
                patch.object(processing, "_find_recent_sent_message_for_conversation", return_value=sent_message), \
                patch("email_automation.messaging.index_message_id", return_value=True), \
                patch("email_automation.messaging.lookup_thread_by_message_id", return_value="thread-1"), \
                patch("email_automation.messaging.index_conversation_id", return_value=True), \
                patch("email_automation.messaging.save_message", side_effect=lambda *_args: saved_messages.append(_args)), \
                patch("email_automation.processing.is_contact_opted_out", return_value=None):
            sent = processing.send_reply_in_thread(
                "uid-1",
                {"Authorization": "Bearer token"},
                "Hi Broker,\n\nThanks.",
                "msg-1",
                "bp21harrison@gmail.com",
                "thread-1",
            )

        self.assertTrue(sent)
        self.assertTrue(any(url.endswith("/createReplyAll") for url in posts))
        self.assertTrue(any(url.endswith("/me/messages/msg-1") for url in gets))
        self.assertTrue(any(url.endswith("/me/messages/auto-reply-draft-1") for url in gets))
        patch_payload = patch_payloads[0]
        self.assertEqual(
            [r["emailAddress"]["address"] for r in patch_payload["toRecipients"]],
            ["bp21harrison@gmail.com"],
        )
        self.assertEqual(patch_payload["ccRecipients"], [])
        self.assertEqual(saved_messages[0][3]["to"], ["bp21harrison@gmail.com"])
        self.assertEqual(saved_messages[0][3]["cc"], [])


class AutomaticReplySendCounterTests(unittest.TestCase):
    USER_ID = "uid-counter"
    THREAD_ID = "thread-counter"
    MESSAGE_ID = "message-counter"
    RECIPIENT = "broker@example.com"

    def _run_reply(
        self,
        *,
        initial_user_count=10,
        initial_global_count=10,
        send_result=202,
        sent_message_found=True,
        fail_reservation=False,
        delete_result=204,
    ):
        day_key = email_module._send_counter_day_key()
        user_counter_path = (
            "users", self.USER_ID, "sendCounters", day_key,
        )
        global_counter_path = ("sendCounters", f"global-{day_key}")
        fake_fs = SendCounterFirestore({
            ("users", self.USER_ID): {"email": "sender@example.com"},
            ("users", self.USER_ID, "threads", self.THREAD_ID): {
                "clientId": "client-counter",
                "status": "active",
            },
            user_counter_path: {"count": initial_user_count},
            global_counter_path: {"count": initial_global_count},
        })
        if fail_reservation:
            fake_fs.fail_next_transaction()
        post_urls = []
        deleted_urls = []

        def fake_post(url, **_kwargs):
            post_urls.append(url)
            if url.endswith("/createReplyAll"):
                return FakeResponse(201, {
                    "id": "reply-draft-counter",
                    "toRecipients": [{
                        "emailAddress": {"address": self.RECIPIENT},
                    }],
                    "ccRecipients": [],
                })
            if url.endswith("/reply-draft-counter/send"):
                if send_result == "uncertain":
                    raise processing.requests.Timeout("provider outcome unknown")
                return FakeResponse(send_result)
            raise AssertionError(f"Unexpected POST {url}")

        def fake_get(url, **_kwargs):
            if url.endswith(f"/me/messages/{self.MESSAGE_ID}"):
                return FakeResponse(200, {
                    "conversationId": "conversation-counter",
                    "subject": "RE: 100 Counter Way",
                    "from": {
                        "emailAddress": {"address": self.RECIPIENT},
                    },
                    "toRecipients": [{
                        "emailAddress": {"address": "sender@example.com"},
                    }],
                    "ccRecipients": [],
                })
            raise AssertionError(f"Unexpected GET {url}")

        def fake_backoff(callback, **_kwargs):
            response = callback()
            if response.status_code >= 400:
                raise processing.requests.exceptions.HTTPError(
                    f"HTTP {response.status_code}", response=response,
                )
            return response

        sent_message = {
            "id": "sent-counter",
            "internetMessageId": "<sent-counter@example.com>",
            "conversationId": "conversation-counter",
            "subject": "RE: 100 Counter Way",
            "sentDateTime": "2026-08-09T18:00:00Z",
            "toRecipients": [{
                "emailAddress": {"address": self.RECIPIENT},
            }],
            "ccRecipients": [],
            "body": {"contentType": "HTML", "content": "Thanks"},
            "bodyPreview": "Thanks",
        }
        allow = CampaignAutomationDecision(
            state="allow",
            reason="",
            client_data={"status": "live"},
            metadata={"terminal": False, "stopKind": "none"},
        )

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "SITESIFT_AUTO_REPLY_ALLOWLIST": self.USER_ID,
                "SITESIFT_DAILY_SEND_CAP": "20",
                "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
                "SITESIFT_OUTBOUND_MODE": "live",
            }))
            stack.enter_context(patch(
                "email_automation.utils.exponential_backoff_request",
                side_effect=fake_backoff,
            ))
            stack.enter_context(patch.object(
                email_module.firestore, "transactional", fake_transactional,
            ))
            stack.enter_context(patch("email_automation.clients._fs", fake_fs))
            stack.enter_context(patch.object(
                processing, "get_client_automation_decision", return_value=allow,
            ))
            stack.enter_context(patch.object(
                processing.requests, "get", side_effect=fake_get,
            ))
            stack.enter_context(patch.object(
                processing.requests, "post", side_effect=fake_post,
            ))
            stack.enter_context(patch.object(
                processing.requests, "patch", return_value=FakeResponse(204),
            ))
            stack.enter_context(patch.object(
                processing.requests,
                "delete",
                side_effect=lambda url, **_kwargs: (
                    deleted_urls.append(url) or FakeResponse(delete_result)
                ),
            ))
            stack.enter_context(patch.object(processing.time, "sleep", return_value=None))
            stack.enter_context(patch.object(
                processing,
                "_find_recent_sent_message_for_conversation",
                return_value=sent_message if sent_message_found else None,
            ))
            stack.enter_context(patch(
                "email_automation.messaging.index_message_id", return_value=True,
            ))
            stack.enter_context(patch(
                "email_automation.messaging.lookup_thread_by_message_id",
                return_value=self.THREAD_ID,
            ))
            stack.enter_context(patch(
                "email_automation.messaging.index_conversation_id", return_value=True,
            ))
            stack.enter_context(patch("email_automation.messaging.save_message"))
            stack.enter_context(patch(
                "email_automation.processing.is_contact_opted_out", return_value=None,
            ))
            sent = processing.send_reply_in_thread(
                self.USER_ID,
                {"Authorization": "Bearer local-test"},
                "Hi Broker,\n\nThanks for the update.",
                self.MESSAGE_ID,
                self.RECIPIENT,
                self.THREAD_ID,
            )

        return {
            "sent": sent,
            "user_count": fake_fs.count(*user_counter_path),
            "global_count": fake_fs.count(*global_counter_path),
            "post_urls": post_urls,
            "deleted_urls": deleted_urls,
            "outcome": processing.send_reply_in_thread.last_outcome,
            "error": processing.send_reply_in_thread.last_error,
        }

    def test_confirmed_automatic_reply_advances_both_counters_from_10_to_11(self):
        result = self._run_reply()

        self.assertTrue(result["sent"])
        self.assertEqual(11, result["user_count"])
        self.assertEqual(11, result["global_count"])

    def test_definite_client_rejection_refunds_both_counters(self):
        result = self._run_reply(send_result=400)

        self.assertFalse(result["sent"])
        self.assertEqual(10, result["user_count"])
        self.assertEqual(10, result["global_count"])

    def test_ambiguous_http_rejections_conservatively_keep_both_reservations(self):
        for status in (408, 409, 425, 429, 500):
            with self.subTest(status=status):
                result = self._run_reply(send_result=status)

                self.assertFalse(result["sent"])
                self.assertEqual(11, result["user_count"])
                self.assertEqual(11, result["global_count"])

    def test_any_provider_2xx_is_accepted_and_keeps_both_reservations(self):
        result = self._run_reply(send_result=204)

        self.assertTrue(result["sent"])
        self.assertEqual(11, result["user_count"])
        self.assertEqual(11, result["global_count"])

    def test_unknown_provider_outcome_conservatively_keeps_both_reservations(self):
        result = self._run_reply(send_result="uncertain")

        self.assertFalse(result["sent"])
        self.assertEqual(11, result["user_count"])
        self.assertEqual(11, result["global_count"])

    def test_atomic_reservation_failure_blocks_send_without_splitting_ledgers(self):
        result = self._run_reply(fail_reservation=True)

        self.assertFalse(result["sent"])
        self.assertFalse(any(url.endswith("/send") for url in result["post_urls"]))
        self.assertEqual(10, result["user_count"])
        self.assertEqual(10, result["global_count"])

    def test_two_reservations_at_19_allow_only_one(self):
        day_key = email_module._send_counter_day_key()
        user_path = ("users", self.USER_ID, "sendCounters", day_key)
        global_path = ("sendCounters", f"global-{day_key}")
        fake_fs = SendCounterFirestore({
            user_path: {"count": 19},
            global_path: {"count": 19},
        })

        with patch.dict(os.environ, {
            "SITESIFT_DAILY_SEND_CAP": "20",
            "SITESIFT_GLOBAL_DAILY_SEND_CAP": "20",
        }), patch.object(email_module.firestore, "transactional", fake_transactional):
            first = email_module._check_single_provider_send_cap(fake_fs, self.USER_ID)
            second = email_module._check_single_provider_send_cap(fake_fs, self.USER_ID)

        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertEqual(20, fake_fs.count(*user_path))
        self.assertEqual(20, fake_fs.count(*global_path))

    def test_user_and_global_caps_each_block_before_provider_send(self):
        cases = (
            {"initial_user_count": 20, "initial_global_count": 10},
            {"initial_user_count": 10, "initial_global_count": 20},
        )
        for counts in cases:
            with self.subTest(**counts):
                result = self._run_reply(**counts)

                self.assertFalse(result["sent"])
                self.assertFalse(any(
                    url.endswith("/send") for url in result["post_urls"]
                ))
                self.assertEqual(
                    counts["initial_user_count"], result["user_count"],
                )
                self.assertEqual(
                    counts["initial_global_count"], result["global_count"],
                )
                self.assertTrue(result["deleted_urls"])

    def test_confirmed_send_counts_before_later_sent_items_index_failure(self):
        result = self._run_reply(sent_message_found=False)

        self.assertFalse(result["sent"])
        self.assertEqual("sent_but_unindexed", result["outcome"])
        self.assertEqual(11, result["user_count"])
        self.assertEqual(11, result["global_count"])

    def test_cap_blocked_false_queues_unresolved_work_instead_of_resolving_send(self):
        result = self._run_reply(initial_user_count=20)

        self.assertFalse(result["sent"])
        with patch.object(processing, "queue_pending_response") as queue_pending, \
                patch.object(processing, "record_sent_unindexed_response") as reconcile, \
                patch.object(processing, "update_thread_status") as keep_active:
            resolved = processing._handle_auto_response_send_failure(
                self.USER_ID,
                self.THREAD_ID,
                self.MESSAGE_ID,
                self.RECIPIENT,
                "Hi Broker,\n\nThanks for the update.",
                "client-counter",
                failure_label="closing email",
                terminal_reason="all_fields_gathered",
                terminal_row_number=7,
            )

        self.assertFalse(resolved)
        queue_pending.assert_called_once()
        self.assertEqual(
            "all_fields_gathered", queue_pending.call_args.kwargs["terminal_reason"],
        )
        self.assertEqual(7, queue_pending.call_args.kwargs["terminal_row_number"])
        reconcile.assert_not_called()
        keep_active.assert_called_once_with(
            self.USER_ID, self.THREAD_ID, processing.THREAD_STATUS["active"],
            "daily_send_cap_deferred",
        )

        with patch.object(processing, "_maybe_mark_client_completed") as mark_complete:
            completed = processing._complete_client_after_deferred_reply(
                self.USER_ID, "client-counter",
            )
        self.assertFalse(completed)
        mark_complete.assert_not_called()

    def test_deferred_closing_retry_restores_terminal_state_before_completion(self):
        events = []
        thread_path = ("users", self.USER_ID, "threads", self.THREAD_ID)
        sibling_path = ("users", self.USER_ID, "threads", "thread-sibling")
        client_path = ("users", self.USER_ID, "clients", "client-counter")
        action_path = client_path + ("notifications", "action-current")
        fake_fs = SendCounterFirestore({
            client_path: {"status": "live"},
            thread_path: {
                "clientId": "client-counter", "rowNumber": 7,
                "status": "active", "statusReason": "daily_send_cap_deferred",
            },
            sibling_path: {
                "clientId": "client-counter", "rowNumber": 7, "status": "active",
            },
            action_path: {"kind": "action_needed"},
        })
        pending_doc = MagicMock(id=self.THREAD_ID)

        def delete_pending():
            restored = fake_fs.store[thread_path]
            self.assertEqual("completed", restored["status"])
            self.assertEqual("all_fields_gathered", restored["statusReason"])
            self.assertEqual("stopped", restored["followUpStatus"])
            self.assertIsNone(restored["followUpConfig.processingBy"])
            self.assertIsNone(restored["followUpConfig.processingAt"])
            self.assertEqual("stopped", fake_fs.store[sibling_path]["status"])
            self.assertNotIn(action_path, fake_fs.store)
            events.append("delete")

        pending_doc.reference.delete.side_effect = delete_pending
        data = {
            "threadId": self.THREAD_ID, "msgId": self.MESSAGE_ID,
            "recipient": self.RECIPIENT, "responseBody": "Thanks",
            "clientId": "client-counter", "attempts": 0,
            "terminalDisposition": {
                "status": "completed", "reason": "all_fields_gathered",
                "rowNumber": 7,
            },
        }
        allow = CampaignAutomationDecision(
            state="allow", reason="", client_data={},
            metadata={"terminal": False, "stopKind": "none"},
        )

        def clear_actions(*_args, **_kwargs):
            fake_fs.store.pop(action_path, None)

        def complete_row(_user_id, row_number, *, client_id, reason, strict=False):
            self.assertTrue(strict)
            self.assertEqual((7, "client-counter", "all_fields_gathered"), (
                row_number, client_id, reason,
            ))
            fake_fs.store[sibling_path].update({
                "status": "stopped", "statusReason": reason,
                "followUpStatus": "stopped",
            })
            return 2

        with patch.object(
            pending_responses, "get_pending_responses",
            return_value=[{"doc": pending_doc, "data": data}],
        ), patch.object(
            pending_responses, "get_client_automation_decision", return_value=allow,
        ), patch.object(
            pending_responses, "_gate_pending_response", return_value=False,
        ), patch.object(
            pending_responses, "validate_outbound_body",
            return_value=SimpleNamespace(is_safe=True),
        ), patch.object(
            processing, "send_reply_in_thread", return_value=True,
        ) as send_reply, patch.object(
            processing, "_fs", fake_fs,
        ), patch.object(
            processing, "_clear_thread_action_notifications", side_effect=clear_actions,
        ) as clear, patch.object(
            processing, "complete_threads_for_row", side_effect=complete_row,
        ):
            states = pending_responses.process_pending_responses(
                self.USER_ID, {"Authorization": "Bearer local-test"},
            )

        self.assertEqual(["delete"], events)
        send_reply.assert_called_once()
        clear.assert_called_once_with(
            self.USER_ID, "client-counter", self.THREAD_ID, strict=True,
        )
        self.assertEqual("completed", fake_fs.store[client_path]["status"])
        self.assertEqual("healthy", states[0]["status"])

    def test_failed_deferred_closing_retry_keeps_active_pending_state(self):
        pending_doc = MagicMock(id=self.THREAD_ID)
        data = {
            "threadId": self.THREAD_ID, "msgId": self.MESSAGE_ID,
            "recipient": self.RECIPIENT, "responseBody": "Thanks",
            "clientId": "client-counter", "attempts": 0,
            "terminalDisposition": {
                "status": "completed", "reason": "all_fields_gathered",
                "rowNumber": 7,
            },
        }
        allow = CampaignAutomationDecision(
            state="allow", reason="", client_data={},
            metadata={"terminal": False, "stopKind": "none"},
        )

        def fail_unknown(**_kwargs):
            processing._set_reply_send_outcome(
                error="provider outcome unknown", outcome="send_failed",
            )
            return False

        with patch.object(
            pending_responses, "get_pending_responses",
            return_value=[{"doc": pending_doc, "data": data}],
        ), patch.object(
            pending_responses, "get_client_automation_decision", return_value=allow,
        ), patch.object(
            pending_responses, "_gate_pending_response", return_value=False,
        ), patch.object(
            pending_responses, "validate_outbound_body",
            return_value=SimpleNamespace(is_safe=True),
        ), patch.object(
            processing, "send_reply_in_thread", side_effect=fail_unknown,
        ), patch.object(
            processing, "_restore_deferred_terminal_reply",
        ) as restore, patch.object(
            processing, "_maybe_mark_client_completed",
        ) as complete:
            states = pending_responses.process_pending_responses(
                self.USER_ID, {"Authorization": "Bearer local-test"},
            )

        restore.assert_not_called()
        complete.assert_not_called()
        pending_doc.reference.delete.assert_not_called()
        self.assertEqual(1, pending_doc.reference.update.call_args.args[0]["attempts"])
        self.assertEqual("error", states[0]["status"])

    def test_confirmed_delivery_retries_lifecycle_without_second_provider_send(self):
        thread_path = ("users", self.USER_ID, "threads", self.THREAD_ID)
        sibling_path = ("users", self.USER_ID, "threads", "thread-sibling")
        client_path = ("users", self.USER_ID, "clients", "client-counter")
        action_path = client_path + ("notifications", "action-current")
        fake_fs = SendCounterFirestore({
            client_path: {"status": "live"},
            thread_path: {
                "clientId": "client-counter", "rowNumber": 7,
                "status": "active", "statusReason": "daily_send_cap_deferred",
            },
            sibling_path: {
                "clientId": "client-counter", "rowNumber": 7, "status": "active",
            },
            action_path: {
                "kind": "action_needed", "threadId": self.THREAD_ID,
            },
        })
        pending_doc = MagicMock(id=self.THREAD_ID)
        data = {
            "threadId": self.THREAD_ID, "msgId": self.MESSAGE_ID,
            "recipient": self.RECIPIENT, "responseBody": "Thanks",
            "clientId": "client-counter", "attempts": 0,
            "terminalDisposition": {
                "status": "completed", "reason": "all_fields_gathered", "rowNumber": 7,
            },
        }
        pending_doc.reference.update.side_effect = lambda payload: data.update(payload)
        delete_attempts = 0

        def delete_action(*_args):
            nonlocal delete_attempts
            delete_attempts += 1
            if delete_attempts == 1:
                raise RuntimeError("notification transaction unavailable")
            fake_fs.store.pop(action_path, None)
            return True

        allow = CampaignAutomationDecision(
            state="allow", reason="", client_data={},
            metadata={"terminal": False, "stopKind": "none"},
        )

        common_patches = (
            patch.object(
                pending_responses, "get_pending_responses",
                return_value=[{"doc": pending_doc, "data": data}],
            ),
            patch.object(
                pending_responses, "get_client_automation_decision", return_value=allow,
            ),
            patch.object(pending_responses, "_gate_pending_response", return_value=False),
            patch.object(
                pending_responses, "validate_outbound_body",
                return_value=SimpleNamespace(is_safe=True),
            ),
            patch.object(pending_responses, "find_matching_sent_message_for_retry", return_value=None),
            patch.object(pending_responses, "find_sent_conversation_continuation_for_retry", return_value=None),
        )
        with ExitStack() as stack:
            for context in common_patches:
                stack.enter_context(context)
            send = stack.enter_context(patch.object(
                processing, "send_reply_in_thread", return_value=True,
            ))
            stack.enter_context(patch.object(processing, "_fs", fake_fs))
            stack.enter_context(patch.object(sheet_operations, "_fs", fake_fs))
            stack.enter_context(patch.object(
                processing, "delete_notification_and_decrement_counters",
                side_effect=delete_action,
            ))
            complete = stack.enter_context(patch.object(
                processing, "_maybe_mark_client_completed", return_value=True,
            ))
            pending_responses.process_pending_responses(
                self.USER_ID, {"Authorization": "Bearer local-test"},
            )
            pending_responses.process_pending_responses(
                self.USER_ID, {"Authorization": "Bearer local-test"},
            )

        self.assertTrue(data["deliveryConfirmed"])
        send.assert_called_once()
        self.assertEqual(2, delete_attempts)
        self.assertEqual("completed", fake_fs.store[thread_path]["status"])
        self.assertEqual("completed", fake_fs.store[sibling_path]["status"])
        self.assertNotIn(action_path, fake_fs.store)
        pending_doc.reference.delete.assert_called_once()
        complete.assert_called_once_with(self.USER_ID, "client-counter")

    def test_nonstandard_confirmed_deliveries_mark_before_terminal_lifecycle(self):
        for classification in ("prior_sent", "sent_but_unindexed"):
            with self.subTest(classification=classification):
                pending_doc = MagicMock(id=self.THREAD_ID)
                data = {
                    "threadId": self.THREAD_ID, "msgId": self.MESSAGE_ID,
                    "recipient": self.RECIPIENT, "responseBody": "Thanks",
                    "clientId": "client-counter",
                    "attempts": 1 if classification == "prior_sent" else 0,
                    "lastError": "unknown" if classification == "prior_sent" else None,
                    "terminalDisposition": {
                        "status": "completed", "reason": "all_fields_gathered",
                        "rowNumber": 7,
                    },
                }
                pending_doc.reference.update.side_effect = lambda payload: data.update(payload)
                allow = CampaignAutomationDecision(
                    state="allow", reason="", client_data={},
                    metadata={"terminal": False, "stopKind": "none"},
                )

                def assert_marked_before_lifecycle(*_args):
                    self.assertTrue(data.get("deliveryConfirmed"))

                def send_result(**_kwargs):
                    if classification == "prior_sent":
                        raise AssertionError("prior Sent Items match must prevent provider retry")
                    processing._set_reply_send_outcome(
                        error="accepted but not indexed", sent_but_unindexed=True,
                        outcome="sent_but_unindexed",
                    )
                    return False

                sent_match = {"id": "sent-1"} if classification == "prior_sent" else None
                with patch.object(
                    pending_responses, "get_pending_responses",
                    return_value=[{"doc": pending_doc, "data": data}],
                ), patch.object(
                    pending_responses, "get_client_automation_decision", return_value=allow,
                ), patch.object(
                    pending_responses, "_gate_pending_response", return_value=False,
                ), patch.object(
                    pending_responses, "validate_outbound_body",
                    return_value=SimpleNamespace(is_safe=True),
                ), patch.object(
                    pending_responses, "find_matching_sent_message_for_retry",
                    return_value=sent_match,
                ), patch.object(
                    processing, "send_reply_in_thread", side_effect=send_result,
                ), patch.object(
                    processing, "_restore_deferred_terminal_reply",
                    side_effect=assert_marked_before_lifecycle,
                ), patch.object(
                    pending_responses, "record_sent_unindexed_response",
                ) as reconcile:
                    pending_responses.process_pending_responses(
                        self.USER_ID, {"Authorization": "Bearer local-test"},
                    )

                self.assertTrue(data["deliveryConfirmed"])
                reconcile.assert_called_once()
                pending_doc.reference.delete.assert_called_once()

    def test_cap_block_surfaces_abandoned_draft_cleanup_failure(self):
        result = self._run_reply(initial_user_count=20, delete_result=500)

        self.assertFalse(result["sent"])
        self.assertIn("draft cleanup failed", result["error"])


if __name__ == "__main__":
    unittest.main()
