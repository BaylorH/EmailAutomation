"""
GO-condition #3: per-item send-failure observability.

Today, a single item's Graph *send* failure inside send_outboxes /
process_pending_responses / check_and_send_followups is swallowed and never
reaches the health rail. These tests pin the new contract: each of the three
functions returns a list of Graph operation-states (the same shape
`main._combine_graph_operation_states` already consumes), and a swallowed
per-item send failure produces an "error" op-state that escalates the rail.

No live Graph/Gmail calls: the Graph send boundary is mocked in every test.
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
# Resolve service-account.json relative to the repo root instead of hardcoding a
# developer-specific absolute path. setdefault keeps any external/CI override
# authoritative, and os.path.exists lets this fall through cleanly when the file
# is absent (e.g. CI without secrets provisioned).
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_candidate_credentials = os.path.join(_repo_root, "service-account.json")
if os.path.exists(_candidate_credentials):
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _candidate_credentials)

import main
from email_automation import email as email_module
from email_automation import followup as followup_module
from email_automation import pending_responses


# ---------------------------------------------------------------------------
# Minimal Firestore/document fakes
# ---------------------------------------------------------------------------
class FakeDocRef:
    def __init__(self, doc_id="doc-1"):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []
        self.update_calls = []

    def delete(self):
        self.deleted = True

    def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))

    def update(self, data):
        self.update_calls.append(data)


class FakeDoc:
    def __init__(self, data, doc_id="doc-1"):
        self.id = doc_id
        self.reference = FakeDocRef(doc_id)
        self._data = data

    def to_dict(self):
        return self._data


class FakeOutboxCollection:
    def __init__(self, docs):
        self.docs = docs

    def order_by(self, _field):
        return self

    def stream(self):
        return self.docs


class FakeSendCounterDoc:
    """Today's per-user send counter: absent -> count 0 -> under the daily cap."""

    def get(self):
        return types.SimpleNamespace(exists=False, to_dict=lambda: {})

    def set(self, *args, **kwargs):
        return None


class FakeSendCounterCollection:
    def document(self, _day_key):
        return FakeSendCounterDoc()


class FakeUserNode:
    def __init__(self, docs):
        self.docs = docs

    def get(self):
        return types.SimpleNamespace(exists=False, to_dict=lambda: {})

    def collection(self, name):
        # #15/#18 Rail-2 daily send-cap reads a sendCounters collection before
        # sending; seed it (empty -> count 0 -> under cap) so the cap does not
        # spuriously fail-closed and retain the outbox in this observability test.
        if name == email_module.SEND_COUNTERS_COLLECTION:
            return FakeSendCounterCollection()
        assert name == "outbox", name
        return FakeOutboxCollection(self.docs)


class FakeUsersCollection:
    def __init__(self, docs):
        self.docs = docs

    def document(self, _user_id):
        return FakeUserNode(self.docs)


class FakeOutboxFirestore:
    def __init__(self, docs):
        self.docs = docs

    def collection(self, name):
        assert name == "users", name
        return FakeUsersCollection(self.docs)


# ---------------------------------------------------------------------------
# 1. send_outboxes / _send_single_outbox_item
# ---------------------------------------------------------------------------
class SendOutboxSendFailureObservabilityTests(unittest.TestCase):
    def _drive_single_item_send_failure(self, operation_states):
        doc = FakeDoc(
            {
                "assignedEmails": ["broker@example.com"],
                "script": "Hi there,\n\nAny update on the space?\n\nThanks",
                "clientId": "",
                "subject": "100 Observability Way",
                "attempts": 0,
                "scriptSelectionMode": "exact",
            },
            doc_id="outbox-fail",
        )

        graph_error_result = {
            "sent": [],
            "errors": {"broker@example.com": "Graph 500 Internal Server Error"},
        }

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value={}), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_pause_results_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_dead_letter_campaign_recipient_row_mismatch_if_needed", return_value=False), \
             patch.object(email_module, "_dead_letter_unsafe_outbound_body_if_needed", return_value=False), \
             patch.object(email_module, "_dead_letter_unresolved_name_placeholder_if_needed", return_value=False), \
             patch.object(email_module, "_should_use_exact_outbox_script", return_value=True), \
             patch.object(email_module, "_sent_retry_reconciliation_result", return_value={}), \
             patch.object(email_module, "_mark_outbox_action_audit_retrying"), \
             patch.object(email_module, "send_and_index_email", return_value=graph_error_result) as send:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
                operation_states=operation_states,
            )
        # The Graph send boundary was actually exercised (and failed).
        send.assert_called_once()
        return doc

    def _drive_single_item_send_success(self, operation_states):
        doc = FakeDoc(
            {
                "assignedEmails": ["broker@example.com"],
                "script": "Hi there,\n\nAny update on the space?\n\nThanks",
                "clientId": "",
                "subject": "100 Observability Way",
                "attempts": 0,
                "scriptSelectionMode": "exact",
            },
            doc_id="outbox-ok",
        )

        graph_ok_result = {
            "sent": ["broker@example.com"],
            "errors": {},
        }

        with patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value={}), \
             patch.object(email_module, "_has_existing_thread_for_property", return_value=False), \
             patch.object(email_module, "_pause_results_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_pause_client_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_dead_letter_campaign_recipient_row_mismatch_if_needed", return_value=False), \
             patch.object(email_module, "_dead_letter_unsafe_outbound_body_if_needed", return_value=False), \
             patch.object(email_module, "_dead_letter_unresolved_name_placeholder_if_needed", return_value=False), \
             patch.object(email_module, "_should_use_exact_outbox_script", return_value=True), \
             patch.object(email_module, "_sent_retry_reconciliation_result", return_value={}), \
             patch.object(email_module, "_mark_outbox_action_audit_retrying"), \
             patch.object(email_module, "send_and_index_email", return_value=graph_ok_result) as send:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": doc.to_dict()},
                operation_states=operation_states,
            )
        send.assert_called_once()
        return doc

    def test_single_outbox_item_send_success_appends_healthy_state(self):
        states = []
        self._drive_single_item_send_success(states)

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["status"], "healthy")
        self.assertEqual(states[0]["operation"], "outbox_send")
        self.assertNotIn("error", states[0])
        # Health rail stays green when every item sends cleanly.
        self.assertEqual(main._combine_graph_operation_states(states)["status"], "healthy")

    def test_single_outbox_item_send_failure_appends_error_state(self):
        states = []
        self._drive_single_item_send_failure(states)

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["status"], "error")
        self.assertEqual(states[0]["operation"], "outbox_send")
        self.assertIn("Graph 500", states[0]["error"])

    def test_single_outbox_item_send_failure_escalates_health_rail(self):
        states = []
        self._drive_single_item_send_failure(states)

        combined = main._combine_graph_operation_states(states)
        self.assertEqual(combined["status"], "error")
        self.assertEqual(combined["failedOperations"][0]["operation"], "outbox_send")

    def test_send_outboxes_returns_operation_states_list(self):
        docs = [
            FakeDoc(
                {
                    "assignedEmails": ["broker@example.com"],
                    "script": "Hi there",
                    "clientId": "",
                    "subject": "200 Observability Way",
                },
                doc_id="outbox-1",
            )
        ]

        def fake_single(_uid, _headers, _item, *_args, operation_states=None, **_kwargs):
            if operation_states is not None:
                operation_states.append(
                    {"status": "error", "operation": "outbox_send", "error": "Graph 503"}
                )

        with patch("email_automation.clients._fs", FakeOutboxFirestore(docs)), \
             patch.object(email_module, "_send_single_outbox_item", side_effect=fake_single):
            result = email_module.send_outboxes("uid-1", {"Authorization": "Bearer token"})

        self.assertIsInstance(result, list)
        self.assertTrue(any(s.get("status") == "error" for s in result))
        self.assertEqual(main._combine_graph_operation_states(result)["status"], "error")


# ---------------------------------------------------------------------------
# 2. process_pending_responses
# ---------------------------------------------------------------------------
class PendingResponseSendFailureObservabilityTests(unittest.TestCase):
    def _make_fs(self, docs):
        class _Collection:
            def __init__(self, d):
                self.docs = d

            def stream(self):
                return list(self.docs)

            def add(self, _data):
                return FakeDocRef()

        class _FS:
            def __init__(self, d):
                self.collections = {
                    "pendingResponses": _Collection(d),
                    "deadLetterQueue": _Collection([]),
                }

            def document(self, _name):
                return self

            def collection(self, name):
                if name == "users":
                    return self
                return self.collections.setdefault(name, _Collection([]))

        return _FS(docs)

    def test_process_pending_responses_returns_error_state_on_send_failure(self):
        active_doc = FakeDoc(
            {
                "threadId": "thread-1",
                "msgId": "message-1",
                "recipient": "broker@example.com",
                "responseBody": "Hi,\n\nCan you share the flyer?",
                "clientId": "client-1",
                "attempts": 0,
            },
            doc_id="thread-1",
        )
        fake_fs = self._make_fs([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            return False

        fake_send_reply_in_thread.last_error = "HTTP 500 Graph send failed"

        with patch.dict(sys.modules, {
            "email_automation.clients": types.SimpleNamespace(_fs=fake_fs),
            "email_automation.processing": types.SimpleNamespace(
                send_reply_in_thread=fake_send_reply_in_thread,
            ),
        }):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertIsInstance(states, list)
        error_states = [s for s in states if s.get("status") == "error"]
        self.assertEqual(len(error_states), 1)
        self.assertEqual(error_states[0]["operation"], "pending_response_send")
        self.assertIn("Graph send failed", error_states[0]["error"])
        self.assertEqual(main._combine_graph_operation_states(states)["status"], "error")

    def test_process_pending_responses_returns_healthy_state_on_send_success(self):
        active_doc = FakeDoc(
            {
                "threadId": "thread-1",
                "msgId": "message-1",
                "recipient": "broker@example.com",
                "responseBody": "Hi,\n\nCan you share the flyer?",
                "clientId": "client-1",
                "attempts": 0,
            },
            doc_id="thread-1",
        )
        fake_fs = self._make_fs([active_doc])

        def fake_send_reply_in_thread(**_kwargs):
            return True

        with patch.dict(sys.modules, {
            "email_automation.clients": types.SimpleNamespace(_fs=fake_fs),
            "email_automation.processing": types.SimpleNamespace(
                send_reply_in_thread=fake_send_reply_in_thread,
            ),
        }):
            states = pending_responses.process_pending_responses(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["status"], "healthy")
        self.assertEqual(states[0]["operation"], "pending_response_send")
        self.assertEqual(main._combine_graph_operation_states(states)["status"], "healthy")


# ---------------------------------------------------------------------------
# 3. check_and_send_followups
# ---------------------------------------------------------------------------
class FollowupSendFailureObservabilityTests(unittest.TestCase):
    def test_check_and_send_followups_returns_error_state_on_send_failure(self):
        import datetime as _dt

        past = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)

        thread_doc = FakeDoc(
            {
                "clientId": "client-1",
                "followUpStatus": "waiting",
                "followUpConfig": {
                    "enabled": True,
                    "nextFollowUpAt": past,
                    "currentFollowUpIndex": 0,
                    "followUps": [{"message": "Just following up."}],
                },
                "hasInboundReply": False,
            },
            doc_id="thread-followup",
        )

        class _ThreadsQuery:
            def __init__(self, docs):
                self.docs = docs

            def where(self, *_args, **_kwargs):
                return self

            def stream(self):
                return list(self.docs)

        class _FS:
            def __init__(self, docs):
                self.docs = docs

            def collection(self, _name):
                return self

            def document(self, _name):
                return self

            # threads_ref.where(...) path
            def where(self, *_args, **_kwargs):
                return _ThreadsQuery(self.docs)

        fake_fs = _FS([thread_doc])

        def fake_send_followup_email(**_kwargs):
            return False

        fake_send_followup_email.last_error = "HTTP 502 Graph follow-up send failed"

        with patch.object(followup_module, "_fs", fake_fs), \
             patch.object(followup_module, "get_client_automation_pause", return_value=(False, None, {})), \
             patch.object(followup_module, "_next_business_followup_time", side_effect=lambda now, cfg: now), \
             patch.object(followup_module, "_claim_followup", return_value=True), \
             patch.object(followup_module, "_release_followup_claim"), \
             patch.object(followup_module, "_send_followup_email", fake_send_followup_email):
            states = followup_module.check_and_send_followups(
                "uid-1", {"Authorization": "Bearer token"}
            )

        self.assertIsInstance(states, list)
        error_states = [s for s in states if s.get("status") == "error"]
        self.assertEqual(len(error_states), 1)
        self.assertEqual(error_states[0]["operation"], "followup_send")
        self.assertIn("Graph follow-up send failed", error_states[0]["error"])
        self.assertEqual(main._combine_graph_operation_states(states)["status"], "error")


if __name__ == "__main__":
    unittest.main()
