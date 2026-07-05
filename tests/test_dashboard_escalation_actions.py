"""Dashboard user-escalation lifecycle safety tests.

Covers the PAUSED / escalated thread lifecycle that brokers actually create
(call_requested / needs_user_input / wrong_contact) and the dashboard actions
an operator takes on those threads. Everything is driven through the REAL
handlers with a faked Firestore/Graph/Sheets surface — ZERO live sends and
ZERO real Firestore/Sheet writes.
"""

import importlib.util
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation import email as email_module

if importlib.util.find_spec("flask"):
    import app as app_module
else:
    app_module = None


# ---------------------------------------------------------------------------
# Minimal stateful fake Firestore (only the surface the real handlers touch)
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self):
        self.docs = {}          # path tuple -> data dict (or None once deleted)
        self.update_log = []
        self.set_log = []
        self.add_log = []
        self.deleted = []

    def seed(self, path, data):
        self.docs[tuple(path)] = dict(data)

    def data_at(self, path):
        return self.docs.get(tuple(path))

    def collection(self, name):
        return FakeCollection(self, ("col", name))


class FakeCollection:
    def __init__(self, store, path):
        self.store = store
        self.path = path

    def document(self, doc_id):
        return FakeDocRef(self.store, self.path + ("doc", doc_id), doc_id)

    def where(self, field, op, value):
        return FakeQuery(self.store, self.path, [(field, op, value)])

    def stream(self):
        return FakeQuery(self.store, self.path, []).stream()

    def add(self, data):
        doc_id = f"auto-{len(self.store.add_log)}"
        ref = FakeDocRef(self.store, self.path + ("doc", doc_id), doc_id)
        self.store.docs[ref.path] = dict(data)
        self.store.add_log.append((ref.path, dict(data)))
        return (None, ref)


class FakeQuery:
    def __init__(self, store, col_path, filters):
        self.store = store
        self.col_path = col_path
        self.filters = filters

    def where(self, field, op, value):
        return FakeQuery(self.store, self.col_path, self.filters + [(field, op, value)])

    @staticmethod
    def _match(data, f):
        field, op, value = f
        if op == "==":
            return data.get(field) == value
        return False

    def stream(self):
        results = []
        clen = len(self.col_path)
        for path, data in list(self.store.docs.items()):
            if data is None:
                continue
            if len(path) == clen + 2 and path[:clen] == self.col_path and path[clen] == "doc":
                if all(self._match(data, f) for f in self.filters):
                    results.append(FakeDocSnapshot(FakeDocRef(self.store, path, path[-1]), data))
        return results


class FakeDocRef:
    def __init__(self, store, path, doc_id):
        self.store = store
        self.path = path
        self.id = doc_id

    @property
    def reference(self):
        return self

    def collection(self, name):
        return FakeCollection(self.store, self.path + ("col", name))

    def get(self, transaction=None):
        return FakeDocSnapshot(self, self.store.docs.get(self.path))

    def update(self, data):
        existing = self.store.docs.get(self.path)
        if existing is None:
            raise RuntimeError(f"No document to update at {self.path}")
        for key, value in data.items():
            if "." in key:
                parts = key.split(".")
                cursor = existing
                for part in parts[:-1]:
                    nxt = cursor.get(part)
                    if not isinstance(nxt, dict):
                        nxt = {}
                        cursor[part] = nxt
                    cursor = nxt
                cursor[parts[-1]] = value
            else:
                existing[key] = value
        self.store.update_log.append((self.path, dict(data)))

    def set(self, data, merge=False):
        existing = self.store.docs.get(self.path)
        if merge and isinstance(existing, dict):
            existing.update(data)
        else:
            self.store.docs[self.path] = dict(data)
        self.store.set_log.append((self.path, dict(data), merge))

    def delete(self):
        self.store.docs[self.path] = None
        self.store.deleted.append(self.path)


class FakeDocSnapshot:
    def __init__(self, ref, data):
        self.reference = ref
        self._data = data
        self.exists = data is not None
        self.id = ref.id

    def to_dict(self):
        return dict(self._data) if self._data is not None else {}


UID = "uid-1"
THREAD_ID = "thread-escalated-1"
OUTBOX_ID = "outbox-queued-reply-1"
THREAD_PATH = ("col", "users", "doc", UID, "col", "threads", "doc", THREAD_ID)
OUTBOX_PATH = ("col", "users", "doc", UID, "col", "outbox", "doc", OUTBOX_ID)
AUDIT_PATH = ("col", "users", "doc", UID, "col", "actionAudit", "doc", "audit-queued-reply")


@unittest.skipIf(app_module is None, "flask is not installed")
class StopConversationOnEscalatedThreadTests(unittest.TestCase):
    """Scenario 1 [HIGH]: STOP on a paused/escalated thread must halt a queued send."""

    def _seed_paused_thread_with_queued_reply(self):
        store = FakeStore()
        # A thread escalated to a human — the state brokers actually create.
        store.seed(THREAD_PATH, {
            "status": "paused",
            "statusReason": "call_requested",
            "clientId": "client-1",
            "rowNumber": 20,
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": True},
        })
        # An AI reply queued just before escalation (dashboard-approved reply).
        store.seed(OUTBOX_PATH, {
            "assignedEmails": ["broker@example.com"],
            "script": "Hi Ron,\n\nHere are the details you asked for.\n\nThanks",
            "clientId": "client-1",
            "subject": "RE: 0 Gemini Ave, Houston",
            "rowNumber": 20,
            "threadId": THREAD_ID,
            "replyToMessageId": "graph-message-1",
            "conversationId": "conversation-1",
            "actionAuditId": "audit-queued-reply",
            "source": "dashboard_manual_reply",
        })
        return store

    def _stop(self, store):
        with patch("email_automation.clients._fs", store), \
             patch("email_automation.messaging._fs", store), \
             patch("email_automation.clients._get_client_config", return_value=(None, None, None)), \
             patch("email_automation.sheets.clear_row_highlight", return_value=True):
            with app_module.app.test_client() as client:
                return client.post("/api/stop-conversation", json={
                    "uid": UID,
                    "threadId": THREAD_ID,
                    "clientId": "client-1",
                })

    def test_stop_marks_thread_stopped_and_clears_followups(self):
        store = self._seed_paused_thread_with_queued_reply()
        resp = self._stop(store)

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])
        thread = store.data_at(THREAD_PATH)
        self.assertEqual(thread["status"], "stopped")
        self.assertEqual(thread["followUpStatus"], "stopped")
        self.assertIsNone(thread["nextFollowUpAt"])

    def test_stop_cancels_queued_outbox_item_for_thread(self):
        """The core invariant: STOP must flag the thread's queued outbox item so
        the worker's cancel guard fires. Without this, stop-conversation leaves
        the queued send live."""
        store = self._seed_paused_thread_with_queued_reply()
        self._stop(store)

        outbox = store.data_at(OUTBOX_PATH)
        self.assertIsNotNone(outbox, "queued outbox item vanished unexpectedly")
        self.assertTrue(
            email_module._is_cancelled_outbox_item(outbox),
            "stop-conversation left the queued outbox item sendable "
            f"(cancelRequested={outbox.get('cancelRequested')!r}, "
            f"status={outbox.get('status')!r})",
        )

    def test_worker_produces_zero_send_after_stop(self):
        """End-to-end: after STOP, the REAL outbox worker must NOT send the
        queued reply for the stopped thread."""
        store = self._seed_paused_thread_with_queued_reply()
        self._stop(store)

        # Worker later claims the item the operator believed was stopped.
        snap = FakeDocRef(store, OUTBOX_PATH, OUTBOX_ID).get()
        item = {"doc": snap, "data": snap.to_dict()}

        # Claim succeeds (as it would against real Firestore) so the ONLY thing
        # that can stop the send is the cancel guard STOP is responsible for.
        with patch("email_automation.clients._fs", store), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "send_and_index_email") as send_and_index_email, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "_get_reply_message_sender") as get_reply_sender:
            email_module._send_single_outbox_item(
                UID,
                {"Authorization": "Bearer token"},
                item,
            )

        send_and_index_email.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        get_reply_sender.assert_not_called()
        # Cancelled item is deleted and terminalized with a visible audit state.
        self.assertIsNone(store.data_at(OUTBOX_PATH))
        audit = store.data_at(AUDIT_PATH)
        self.assertIsNotNone(audit, "no terminal actionAudit written for cancelled reply")
        self.assertEqual(audit["status"], "cancelled")


# ===========================================================================
# Scenario 1 [MED]: CANCEL pending outbox item @ dashboard updateDoc
# (ConversationsPanel.performCancelOutbox writes cancelRequested=true /
#  status='cancel_requested'). Operator cancels a queued reply at the exact
# moment the worker begins processing it. These tests drive the REAL claim /
# cancel handlers (email.py:699-780 `_claim_outbox_item` + the pre/post-claim
# cancel guards) with faked Firestore/Graph. ZERO live sends, ZERO real writes.
# Invariant: an operator-cancelled item is DELETED with a terminal
# actionAudit='cancelled' and Graph send is NEVER invoked — even when the
# cancel write races the worker's claim.
# ===========================================================================

CANCEL_FIELDS = {"cancelRequested": True, "status": "cancel_requested"}


class _CancelSnapshot:
    def __init__(self, data=None, exists=True):
        self._data = data or {}
        self.exists = exists

    def to_dict(self):
        return dict(self._data)


class _CancelTxn:
    """Records writes issued inside the real @transactional claim body."""

    def __init__(self):
        self.deleted_refs = []
        self.updates = []

    def delete(self, ref):
        self.deleted_refs.append(ref)
        ref.deleted = True

    def update(self, ref, data):
        self.updates.append((ref, dict(data)))


class _CancelOutboxRef:
    """`.get(transaction=...)` is what the worker's claim transaction re-reads
    (i.e. the value the racing dashboard cancel has produced by claim time).
    `.get()` (no txn) models the post-claim non-transactional refresh."""

    def __init__(self, doc_id="outbox-cancel-1", tx_snapshot=None, plain_snapshots=None):
        self.id = doc_id
        self.deleted = False
        self.set_calls = []
        self.update_calls = []
        self._tx_snapshot = tx_snapshot if tx_snapshot is not None else _CancelSnapshot({}, exists=False)
        self._plain_snapshots = list(plain_snapshots or [])

    def get(self, transaction=None):
        if transaction is not None:
            return self._tx_snapshot
        if self._plain_snapshots:
            return self._plain_snapshots.pop(0)
        return _CancelSnapshot({}, exists=False)

    def delete(self):
        self.deleted = True

    def set(self, data, merge=False):
        self.set_calls.append((dict(data), merge))

    def update(self, data):
        self.update_calls.append(dict(data))


class _CancelFSNode:
    def __init__(self, root, path):
        self.root = root
        self.path = path

    def collection(self, name):
        return _CancelFSNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return _CancelFSNode(self.root, self.path + ["document", name])

    def set(self, data, merge=False):
        self.root.set_calls.append((tuple(self.path), dict(data), merge))

    def add(self, data):
        self.root.add_calls.append((tuple(self.path), dict(data)))
        return _CancelFSNode(self.root, self.path + ["document", "auto-id"])

    def get(self):
        return _CancelSnapshot({}, exists=False)


class _CancelFS:
    def __init__(self, transaction=None):
        self._transaction = transaction or _CancelTxn()
        self.set_calls = []
        self.add_calls = []

    def transaction(self):
        return self._transaction

    def collection(self, name):
        return _CancelFSNode(self, ["collection", name])


class _CancelDoc:
    def __init__(self, ref, data):
        self.reference = ref
        self.id = ref.id
        self._data = data

    def to_dict(self):
        return self._data


def _cancel_audit_calls(fake_fs):
    return [c for c in fake_fs.set_calls if "actionAudit" in c[0]]


def _queued_reply_data():
    return {
        "assignedEmails": ["broker@example.com"],
        "script": "Hi Ron,\n\nHere are the details you asked for.\n\nThanks",
        "clientId": "client-1",
        "notificationId": "notification-1",
        "threadId": "thread-1",
        "replyToMessageId": "graph-message-1",
        "conversationId": "conversation-1",
        "actionAuditId": "audit-cancel-1",
        "source": "dashboard_manual_reply",
    }


class ClaimCancelRaceTests(unittest.TestCase):
    """Directly exercise the real _claim_outbox_item (email.py:699-780)."""

    def test_claim_aborts_and_terminalizes_when_cancel_lands_during_claim(self):
        """Cancel becomes visible inside the claim transaction (the narrow TOCTOU at
        email.py:720): claim must abort (return False), transactionally delete the
        doc, and terminalize the action audit 'cancelled' — never claim it to send."""
        pre_claim = _queued_reply_data()  # not yet cancelled when worker arrived
        cancelled = {**pre_claim, **CANCEL_FIELDS}

        tx = _CancelTxn()
        doc_ref = _CancelOutboxRef("outbox-cancel-1", tx_snapshot=_CancelSnapshot(cancelled))
        fake_fs = _CancelFS(transaction=tx)

        with patch("email_automation.clients._fs", fake_fs), \
             patch("google.cloud.firestore.transactional", lambda fn: fn):
            claimed = email_module._claim_outbox_item(doc_ref, pre_claim, user_id=UID)

        self.assertFalse(claimed)
        self.assertIn(doc_ref, tx.deleted_refs)
        self.assertTrue(doc_ref.deleted)
        # Worker must NOT have written a processingBy claim.
        self.assertEqual([], tx.updates)
        audit_calls = _cancel_audit_calls(fake_fs)
        self.assertEqual(1, len(audit_calls))
        audit_path, audit_payload, _merge = audit_calls[0]
        self.assertEqual(
            (
                "collection", "users", "document", UID,
                "collection", "actionAudit", "document", "audit-cancel-1",
            ),
            audit_path,
        )
        self.assertEqual("cancelled", audit_payload["status"])
        self.assertEqual("outbox-cancel-1", audit_payload["outboxId"])

    def test_claim_succeeds_when_not_cancelled(self):
        """Control: with no cancel racing, the real claim writes processingBy and
        returns True — and does NOT emit a spurious cancellation audit."""
        data = _queued_reply_data()
        tx = _CancelTxn()
        doc_ref = _CancelOutboxRef("outbox-cancel-2", tx_snapshot=_CancelSnapshot(data))
        fake_fs = _CancelFS(transaction=tx)

        with patch("email_automation.clients._fs", fake_fs), \
             patch("google.cloud.firestore.transactional", lambda fn: fn):
            claimed = email_module._claim_outbox_item(doc_ref, data, user_id=UID)

        self.assertTrue(claimed)
        self.assertFalse(doc_ref.deleted)
        self.assertEqual([], tx.deleted_refs)
        self.assertEqual(1, len(tx.updates))
        _ref, update_payload = tx.updates[0]
        self.assertEqual(email_module.WORKER_ID, update_payload["processingBy"])
        self.assertIn("processingAt", update_payload)
        self.assertEqual([], _cancel_audit_calls(fake_fs))

    def test_claim_aborts_without_audit_when_no_action_audit_id(self):
        """Cancel-during-claim on an item with no actionAuditId still aborts + deletes;
        no audit doc to terminalize (no crash, no send path)."""
        pre_claim = _queued_reply_data()
        pre_claim.pop("actionAuditId")
        cancelled = {**pre_claim, **CANCEL_FIELDS}
        tx = _CancelTxn()
        doc_ref = _CancelOutboxRef("outbox-cancel-3", tx_snapshot=_CancelSnapshot(cancelled))
        fake_fs = _CancelFS(transaction=tx)

        with patch("email_automation.clients._fs", fake_fs), \
             patch("google.cloud.firestore.transactional", lambda fn: fn):
            claimed = email_module._claim_outbox_item(doc_ref, pre_claim, user_id=UID)

        self.assertFalse(claimed)
        self.assertTrue(doc_ref.deleted)
        self.assertEqual([], _cancel_audit_calls(fake_fs))


class SendSingleCancelRaceTests(unittest.TestCase):
    """End-to-end through the real _send_single_outbox_item lifecycle."""

    def test_cancel_during_claim_blocks_graph_send_end_to_end(self):
        """Cancel lands while the real claim transaction re-reads state: worker aborts
        at claim time and NEVER reaches any Graph send call; item deleted, audit
        terminalized 'cancelled'."""
        pre_claim = _queued_reply_data()
        cancelled = {**pre_claim, **CANCEL_FIELDS}

        tx = _CancelTxn()
        doc_ref = _CancelOutboxRef("outbox-cancel-send", tx_snapshot=_CancelSnapshot(cancelled))
        fake_fs = _CancelFS(transaction=tx)
        doc = _CancelDoc(doc_ref, pre_claim)

        with patch("email_automation.clients._fs", fake_fs), \
             patch("google.cloud.firestore.transactional", lambda fn: fn), \
             patch.object(email_module, "_get_reply_message_sender") as get_reply_sender, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                UID,
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": pre_claim},
            )

        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc_ref.deleted)
        audit_calls = _cancel_audit_calls(fake_fs)
        self.assertTrue(audit_calls, "expected a terminal actionAudit write")
        self.assertEqual("cancelled", audit_calls[-1][1]["status"])
        self.assertEqual("outbox-cancel-send", audit_calls[-1][1]["outboxId"])

    def test_cancel_after_claim_pre_send_blocks_graph_send_end_to_end(self):
        """Claim wins the race (item not cancelled during claim), then the operator's
        cancel lands before send. The post-claim non-transactional refresh
        (email.py:2723/2729) must catch it: delete + audit 'cancelled', no Graph send."""
        pre_claim = _queued_reply_data()
        cancelled_refresh = {**pre_claim, **CANCEL_FIELDS}

        fake_fs = _CancelFS()
        doc_ref = _CancelOutboxRef("outbox-cancel-post")
        doc = _CancelDoc(doc_ref, pre_claim)

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value=cancelled_refresh), \
             patch.object(email_module, "_get_reply_message_sender") as get_reply_sender, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            email_module._send_single_outbox_item(
                UID,
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": pre_claim},
            )

        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()
        self.assertTrue(doc_ref.deleted)
        audit_calls = _cancel_audit_calls(fake_fs)
        self.assertTrue(audit_calls, "expected a terminal actionAudit write")
        self.assertEqual("cancelled", audit_calls[-1][1]["status"])
        self.assertEqual("outbox-cancel-post", audit_calls[-1][1]["outboxId"])


# ---------------------------------------------------------------------------
# Scenario 2 [MED]: DISMISS an escalation notification (NotificationsContext.js
# clearNotification / clearAllClientNotifications, lines 474-511).
#
# The dashboard dismiss is a bare deleteDoc with NO backend route: no
# _terminalize_outbox_action_audit equivalent and no thread transition. Unlike
# CANCEL (writes actionAudit status='cancelled') and unlike a post-send delete
# (deleteNotificationOnSend fires only after the audited send), a bare dismiss
# of a needs_user_input notification silently drops an unresolved broker
# question: the thread stays 'paused' with its notification deleted — invisible
# in the notifications list AND unresolvable.
# ---------------------------------------------------------------------------

DIS_UID = "uid-1"
DIS_CLIENT = "client-1"
DIS_THREAD = "thread-needs-input-1"
DIS_NOTIF = "notif-needs-input-1"
DIS_THREAD_PATH = ("col", "users", "doc", DIS_UID, "col", "threads", "doc", DIS_THREAD)
DIS_NOTIF_PATH = (
    "col", "users", "doc", DIS_UID,
    "col", "clients", "doc", DIS_CLIENT,
    "col", "notifications", "doc", DIS_NOTIF,
)
DIS_AUDIT_PATH = ("col", "users", "doc", DIS_UID, "col", "actionAudit", "doc", "audit-needs-input")


class DismissEscalationNotificationTests(unittest.TestCase):
    """DISMISS lifecycle on a paused/needs_user_input thread."""

    def _seed_paused_thread_with_notification(self):
        store = FakeStore()
        store.seed(DIS_THREAD_PATH, {
            "status": "paused",
            "statusReason": "needs_user_input",
            "clientId": DIS_CLIENT,
        })
        store.seed(DIS_NOTIF_PATH, {
            "kind": "action_needed",
            "priority": "important",
            "meta": {"reason": "needs_user_input"},
            "threadId": DIS_THREAD,
            "clientId": DIS_CLIENT,
        })
        store.seed(("col", "users", "doc", DIS_UID, "col", "clients", "doc", DIS_CLIENT), {
            "notificationsUnread": 1,
            "notifCounts": {"action_needed": 1},
        })
        return store

    def _dismiss_notification(self, store):
        """Reproduce clearNotification: a raw deleteDoc on the notification,
        with no backend route and no thread/audit side effect."""
        store.collection("users").document(DIS_UID) \
            .collection("clients").document(DIS_CLIENT) \
            .collection("notifications").document(DIS_NOTIF).delete()

    def test_reference_operator_reply_clears_escalation_with_audit(self):
        """Reference safe path: sending the reviewed reply on the SAME
        needs_user_input thread writes a terminal actionAudit (status=sent) AND
        resumes the thread out of 'paused' (resumeThreadOnSend). This is the
        audited resolution a bare dismiss silently skips."""
        store = self._seed_paused_thread_with_notification()
        outbox_ref = FakeDocRef(
            store,
            ("col", "users", "doc", DIS_UID, "col", "outbox", "doc", "outbox-reply-1"),
            "outbox-reply-1",
        )
        store.docs[outbox_ref.path] = {"placeholder": True}

        data = {
            "clientId": DIS_CLIENT,
            "notificationId": DIS_NOTIF,
            "notificationClientId": DIS_CLIENT,
            "threadId": DIS_THREAD,
            "actionAuditId": "audit-needs-input",
            "assignedEmails": ["broker@example.com"],
            "deleteNotificationOnSend": True,
            "resumeThreadOnSend": True,
            "reason": "needs_user_input",
        }
        send_result = {
            "sentMessageIds": {"broker@example.com": "graph-msg-1"},
            "internetMessageIds": {"broker@example.com": "<msg-1@example.com>"},
            "sent": ["broker@example.com"],
        }

        with patch("email_automation.clients._fs", store), \
             patch.object(email_module, "delete_notification_and_decrement_counters") as del_notif:
            email_module._finalize_successful_outbox_item(
                DIS_UID, outbox_ref, data, send_result=send_result,
            )

        # Visible terminal audit.
        audit = store.data_at(DIS_AUDIT_PATH)
        self.assertIsNotNone(audit, "no terminal actionAudit written for the sent reply")
        self.assertEqual(audit["status"], "sent")
        self.assertEqual(audit["threadId"], DIS_THREAD)
        # Escalation actually clears out of 'paused'.
        self.assertEqual(store.data_at(DIS_THREAD_PATH)["status"], "active")
        # Notification cleared through the counter-safe backend helper.
        del_notif.assert_called_once_with(DIS_UID, DIS_CLIENT, DIS_NOTIF)

    @unittest.expectedFailure
    def test_dismiss_leaves_no_audit_or_thread_transition(self):
        """SAFETY INVARIANT (currently violated): dismissing a needs_user_input
        notification must leave a terminal actionAudit OR move the thread out of
        'paused'. A bare dismiss does neither, so the escalation is dropped:
        notification gone, thread still 'paused', broker question unanswered and
        now invisible. Expected-failure until a dismiss path (frontend write or
        backend route) terminalizes the action or transitions the thread."""
        store = self._seed_paused_thread_with_notification()

        self._dismiss_notification(store)

        # Notification is gone from the operator's list...
        self.assertIsNone(store.data_at(DIS_NOTIF_PATH))

        # ...but was the escalation actually resolved?
        audit_written = store.data_at(DIS_AUDIT_PATH) is not None or any(
            "actionAudit" in path for path, *_ in store.set_log
        )
        thread_status = (store.data_at(DIS_THREAD_PATH) or {}).get("status")
        escalation_resolved = audit_written or thread_status != "paused"

        self.assertTrue(
            escalation_resolved,
            "dismiss dropped an unresolved escalation: no terminal actionAudit "
            f"and thread still {thread_status!r}",
        )

    def test_dismiss_current_behavior_is_a_silent_drop(self):
        """Characterization (passing): pin the ACTUAL current behavior so the gap
        is regression-visible — a dismiss deletes only the notification and
        touches neither the actionAudit nor the thread status."""
        store = self._seed_paused_thread_with_notification()

        self._dismiss_notification(store)

        self.assertIsNone(store.data_at(DIS_NOTIF_PATH), "notification should be deleted")
        self.assertEqual(DIS_NOTIF_PATH, store.deleted[-1])
        # No terminal audit written.
        self.assertIsNone(store.data_at(DIS_AUDIT_PATH))
        self.assertFalse([p for p, *_ in store.set_log if "actionAudit" in p])
        # Thread remains stuck in 'paused' — the invisible/unresolvable state.
        self.assertEqual(store.data_at(DIS_THREAD_PATH)["status"], "paused")


STALE_ROW = 5       # rowNumber stored on the thread when escalation was raised
CURRENT_ROW = 9     # where the broker's row lives NOW (sheet re-sorted / moved)
BROKER_EMAIL = "broker@example.com"
ROWANCHOR_THREAD_PATH = ("col", "users", "doc", UID, "col", "threads", "doc", "thread-moved-row")


@unittest.skipIf(app_module is None, "flask is not installed")
class ResumeStopRowAnchorTests(unittest.TestCase):
    """Scenario 1 [LOW]: STOP / RESUME on a paused thread whose sheet row MOVED.

    After the escalation was raised, the property row was marked non-viable,
    deleted, or re-sorted in the Google Sheet. The stored ``rowNumber`` is now
    stale. Both handlers must re-resolve the CURRENT row by the thread's
    participant email (the anchor the outbox already uses) and highlight/clear
    that — or no-op when the row is gone — never the stale index.
    """

    def _seed_paused_thread(self, store):
        store.seed(ROWANCHOR_THREAD_PATH, {
            "status": "paused",
            "statusReason": "call_requested",
            "clientId": "client-1",
            "rowNumber": STALE_ROW,
            "email": [BROKER_EMAIL],
            "subject": "RE: 100 Main St, Springfield",
            "followUpStatus": "waiting",
            "followUpConfig": {"enabled": True},
        })

    def _drive(self, route, row_map):
        """Drive a REAL handler with faked Firestore + Sheets.

        row_map maps email -> (rownum_or_None, rowvals) as _find_row_by_email
        would return it from the CURRENT sheet. Returns
        (response, highlight_calls, clear_calls).
        """
        store = FakeStore()
        self._seed_paused_thread(store)
        highlight_calls = []
        clear_calls = []

        def fake_find_row_by_email(_sheets, _sid, _tab, _hdr, email):
            return row_map.get(email, (None, None))

        with patch("email_automation.clients._fs", store), \
             patch("email_automation.messaging._fs", store), \
             patch("email_automation.clients._get_client_config",
                   return_value=("sheet-123", None, None)), \
             patch("email_automation.clients._sheets_client", return_value=object()), \
             patch("email_automation.sheets._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheets._read_header_row2",
                   return_value=["Property Address", "City", "Email"]), \
             patch("email_automation.sheets._find_row_by_email",
                   side_effect=fake_find_row_by_email), \
             patch("email_automation.sheets.highlight_row",
                   side_effect=lambda sid, rn, *a, **k: highlight_calls.append((sid, rn)) or True), \
             patch("email_automation.sheets.clear_row_highlight",
                   side_effect=lambda sid, rn, *a, **k: clear_calls.append((sid, rn)) or True):
            with app_module.app.test_client() as client:
                resp = client.post(route, json={
                    "uid": UID,
                    "threadId": "thread-moved-row",
                    "clientId": "client-1",
                })
        return resp, highlight_calls, clear_calls

    # ----- RESUME -----------------------------------------------------------
    def test_resume_highlights_current_row_not_stale_after_move(self):
        resp, highlight_calls, _clear = self._drive(
            "/api/resume-conversation",
            {BROKER_EMAIL: (CURRENT_ROW, ["100 Main St", "Springfield", BROKER_EMAIL])},
        )
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual("active", resp.get_json()["newStatus"])

        self.assertEqual(1, len(highlight_calls), f"expected one highlight, got {highlight_calls}")
        _sid, painted = highlight_calls[0]
        self.assertEqual(
            CURRENT_ROW, painted,
            f"resume painted stale/wrong row {painted}; broker now lives at {CURRENT_ROW}",
        )
        self.assertNotEqual(STALE_ROW, painted)

    def test_resume_noops_highlight_when_row_removed_as_nonviable(self):
        resp, highlight_calls, _clear = self._drive(
            "/api/resume-conversation",
            {BROKER_EMAIL: (None, None)},  # broker's row gone from the sheet
        )
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(
            [], highlight_calls,
            f"resume painted a stale row for a removed/non-viable property: {highlight_calls}",
        )

    # ----- STOP -------------------------------------------------------------
    def test_stop_clears_current_row_not_stale_after_move(self):
        resp, _highlight, clear_calls = self._drive(
            "/api/stop-conversation",
            {BROKER_EMAIL: (CURRENT_ROW, ["100 Main St", "Springfield", BROKER_EMAIL])},
        )
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual("stopped", resp.get_json()["newStatus"])

        self.assertEqual(1, len(clear_calls), f"expected one clear, got {clear_calls}")
        _sid, cleared = clear_calls[0]
        self.assertEqual(
            CURRENT_ROW, cleared,
            f"stop cleared stale/wrong row {cleared}; broker now lives at {CURRENT_ROW}",
        )
        self.assertNotEqual(STALE_ROW, cleared)

    def test_stop_noops_clear_when_row_removed_as_nonviable(self):
        resp, _highlight, clear_calls = self._drive(
            "/api/stop-conversation",
            {BROKER_EMAIL: (None, None)},
        )
        self.assertEqual(200, resp.status_code)
        self.assertTrue(resp.get_json()["success"])
        self.assertEqual(
            [], clear_calls,
            f"stop cleared a stale row for a removed/non-viable property: {clear_calls}",
        )


# ===========================================================================
# Scenario 1 [HIGH]: CONTINUE MANUALLY (resume / reply) — escalation re-emission
# after manual continuation.
#
# InlineReplyComposer.jsx queues a manual reply with resumeThreadOnSend +
# deleteNotificationOnSend (email.py:1663-1685). On send the worker deletes the
# action_needed notification whose Firestore doc-id IS the dedupe record
# (write_notification stores at doc_id = sha1(dedupe_key); dedupe key is
# per-thread 'call_requested:{thread_id}', scheduler_runner.py:3000/454-455) and
# flips the thread back to "active". Deleting the notification removes the dedupe
# doc, so the next scheduler pass that re-classifies the SAME conversation can
# re-emit call_requested and write_notification will NOT find the dedupe doc,
# re-firing the escalation the operator just resolved. Message-id idempotency
# (processedMessages) covers the inbound message, not event re-emission.
#
# These tests drive the REAL scheduler handler (scheduler_runner.
# process_inbox_message -> write_notification) through the paused/escalated state
# with a path-keyed fake Firestore + faked Graph/Sheets. ZERO live sends, ZERO
# real Firestore/Sheet writes.
# ===========================================================================

# scheduler_runner hard-requires these env vars at import time.
os.environ.setdefault("AZURE_API_APP_ID", "test-client-id")
os.environ.setdefault("AZURE_API_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("FIREBASE_API_KEY", "test-firebase-api-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-api-key")

import hashlib
from unittest.mock import MagicMock

import scheduler_runner


class _SchedSnapshot:
    def __init__(self, exists, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class _SchedRef:
    """Path-keyed ref. Existence/deletion stay consistent across independently
    constructed refs that address the same path — the property the escalation
    dedupe/delete/re-emit cycle turns on."""

    def __init__(self, store, path):
        self._store = store
        self._path = path

    @property
    def id(self):
        return self._path[-1]

    def collection(self, name):
        return _SchedRef(self._store, self._path + (name,))

    def document(self, name):
        return _SchedRef(self._store, self._path + (name,))

    def get(self, transaction=None):
        return _SchedSnapshot(self._path in self._store, self._store.get(self._path))

    def set(self, data, merge=False):
        if merge and self._path in self._store:
            self._store[self._path] = {**self._store[self._path], **data}
        else:
            self._store[self._path] = dict(data)

    def delete(self):
        self._store.pop(self._path, None)


class _SchedTxn:
    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)


class _SchedFS:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return _SchedRef(self.store, (name,))

    def transaction(self):
        return _SchedTxn()


class ContinueManuallyReEscalationTests(unittest.TestCase):
    """Scenario 1 [HIGH]: a call_requested escalation an operator resolved by
    manually continuing the thread must NOT auto re-escalate on the next
    re-classification pass."""

    S_UID = "user-1"
    S_CLIENT = "client-1"
    S_THREAD = "thread-esc-1"
    S_FROM = "broker@example.com"
    S_CONV = "conv-esc-1"
    S_DEDUPE_KEY = f"call_requested:{S_THREAD}"

    def setUp(self):
        self.fs = _SchedFS()
        self.notif_doc_id = hashlib.sha1(self.S_DEDUPE_KEY.encode("utf-8")).hexdigest()
        self.notif_path = (
            "users", self.S_UID, "clients", self.S_CLIENT,
            "notifications", self.notif_doc_id,
        )
        self.client_path = ("users", self.S_UID, "clients", self.S_CLIENT)

        self._patchers = [
            patch.object(scheduler_runner, "_fs", self.fs),
            patch.object(scheduler_runner.firestore, "transactional", lambda fn: fn),
            patch.object(scheduler_runner, "exponential_backoff_request", lambda fn, *a, **k: fn()),
            patch.object(scheduler_runner, "requests", MagicMock()),
            patch.object(scheduler_runner, "lookup_thread_by_conversation_id", return_value=self.S_THREAD),
            patch.object(scheduler_runner, "lookup_thread_by_message_id", return_value=None),
            patch.object(scheduler_runner, "save_message", lambda *a, **k: None),
            patch.object(scheduler_runner, "index_message_id", lambda *a, **k: None),
            patch.object(scheduler_runner, "dump_thread_from_firestore", lambda *a, **k: None),
            patch.object(scheduler_runner, "write_message_order_test", lambda *a, **k: None),
            patch.object(scheduler_runner, "fetch_pdf_attachments", lambda *a, **k: []),
            patch.object(scheduler_runner, "_sheets_client", lambda: MagicMock()),
            patch.object(scheduler_runner, "get_row_anchor", lambda rowvals, header: "123 Main St"),
            patch.object(
                scheduler_runner, "fetch_and_log_sheet_for_thread",
                lambda uid, tid, counterparty_email=None: (
                    self.S_CLIENT, "sheet-1", ["Property Address", "Email"], 5,
                    ["123 Main St", self.S_FROM],
                ),
            ),
            # The classifier keeps seeing the call request in the conversation
            # history and re-emits call_requested — exactly the re-classification
            # the operator's manual reply was supposed to have resolved.
            patch.object(
                scheduler_runner, "propose_sheet_updates",
                lambda *a, **k: {"events": [{"type": "call_requested"}]},
            ),
        ]
        for p in self._patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])

    def _inbound_msg(self, msg_id):
        # Pre-populate internetMessageHeaders (non-empty) so the handler does not
        # fetch headers over Graph; no In-Reply-To -> matches via conversationId.
        scheduler_runner.requests.get.return_value.json.return_value = {
            "body": {"content": "Can you please give me a call?", "contentType": "Text"}
        }
        return {
            "id": msg_id,
            "internetMessageId": f"<{msg_id}@mail>",
            "conversationId": self.S_CONV,
            "subject": "Re: 123 Main St",
            "from": {"emailAddress": {"address": self.S_FROM}},
            "toRecipients": [{"emailAddress": {"address": "jill@example.com"}}],
            "receivedDateTime": "2026-07-05T10:00:00Z",
            "sentDateTime": "2026-07-05T10:00:00Z",
            "bodyPreview": "Can you please give me a call?",
            "internetMessageHeaders": [{"name": "X-Test", "value": "1"}],
        }

    def _run_pass(self, msg_id):
        scheduler_runner.process_inbox_message(
            self.S_UID, {"Authorization": "x"}, self._inbound_msg(msg_id)
        )

    def _simulate_manual_continue(self):
        """Mirror email.py deleteNotificationOnSend + resumeThreadOnSend."""
        # deleteNotificationOnSend: the notification doc IS the dedupe record.
        self.fs.collection("users").document(self.S_UID).collection("clients") \
            .document(self.S_CLIENT).collection("notifications") \
            .document(self.notif_doc_id).delete()
        # resumeThreadOnSend: flip thread back to active.
        self.fs.collection("users").document(self.S_UID).collection("threads") \
            .document(self.S_THREAD).set({"status": "active"}, merge=True)

    def test_call_requested_escalation_emits_single_notification(self):
        """First escalation writes exactly one action_needed notification and
        bumps the unread counter once."""
        self._run_pass("msg-1")

        self.assertIn(self.notif_path, self.fs.store,
                      "call_requested should have created the action_needed notification")
        self.assertEqual(
            self.fs.store[self.notif_path]["meta"]["reason"], "call_requested"
        )
        self.assertEqual(
            self.fs.store.get(self.client_path, {}).get("notificationsUnread"), 1,
            "first escalation should bump unread exactly once",
        )

    def test_manual_continue_does_not_reescalate_resolved_thread(self):
        """THE BUG: after the operator manually continues the thread (notification
        deleted, thread resumed), a re-classification pass must NOT re-fire the
        resolved call_requested escalation."""
        # 1. Escalation fires; operator sees the action_needed notification.
        self._run_pass("msg-1")
        self.assertIn(self.notif_path, self.fs.store)
        self.assertEqual(self.fs.store[self.client_path]["notificationsUnread"], 1)

        # 2. Operator manually replies from the dashboard: notification deleted
        #    (dedupe doc gone), thread resumed to active.
        self._simulate_manual_continue()
        self.assertNotIn(self.notif_path, self.fs.store,
                         "operator send should have removed the notification/dedupe doc")

        # 3. Next scheduler pass re-classifies the SAME conversation on a later
        #    inbound message and the classifier re-emits call_requested.
        self._run_pass("msg-2")

        # SAFETY INVARIANT: the resolved escalation must not re-fire.
        self.assertNotIn(
            self.notif_path, self.fs.store,
            "resolved call_requested escalation was re-emitted after manual "
            "continuation — the deleted dedupe doc left the escalation window open",
        )
        self.assertEqual(
            self.fs.store[self.client_path]["notificationsUnread"], 1,
            "manual continuation must not produce a second unread escalation bump",
        )


if __name__ == "__main__":
    unittest.main()
