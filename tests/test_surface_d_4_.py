"""Surface D — state-permutation rubric closure for core.stop_cancel_dismiss.

Closes three Production-V1 fixture cells for the dashboard stop/cancel/dismiss
lifecycle, each a real (feature x state) permutation exercising the REAL
production functions (no borrowed greens, no assert-nothing shims):

  * bad_placeholder           -> a dismissed queued item whose body still bears
                                 an unresolved outbound placeholder is torn down
                                 by the cancel/dismiss gate and finalized as
                                 "cancelled" (NOT dead_lettered), so the raw
                                 placeholder never reaches a broker and the
                                 dismissal -- not the placeholder guard --
                                 resolves it.
  * manual_continuation       -> an operator's manually-continued reply that was
                                 NOT cancelled (so the cancel gate is a no-op)
                                 is still stopped by the independent client-stop
                                 gate after the client is stopped, so automation
                                 does not resume on top of the manual thread; a
                                 live-client negative control proves the same
                                 pipeline DOES send.
  * operator_visible_failure  -> a stop that cannot cleanly complete surfaces an
                                 operator-visible dead-letter record + terminal
                                 action-audit (status/failureReason/originalDocId/
                                 attempts) so the blocked item is visible and
                                 retryable; a live-client negative control writes
                                 ZERO dead-letter, proving the record is emitted
                                 only on a genuine stop-failure.

Faked Firestore/Graph doubles only; ZERO live sends.
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

import unittest
from unittest.mock import patch

from email_automation import email as email_module
from email_automation.outbound_safety import (
    find_unresolved_placeholders,
    validate_outbound_body,
)


# --------------------------------------------------------------------------- #
# Firestore doubles.
# --------------------------------------------------------------------------- #
class FakeDocRef:
    def __init__(self, doc_id):
        self.id = doc_id
        self.deleted = False

    def delete(self):
        self.deleted = True

    def set(self, data, merge=False):
        self.set_calls = getattr(self, "set_calls", [])
        self.set_calls.append((data, merge))


class FakeDoc:
    def __init__(self, data, doc_id):
        self.id = doc_id
        self.reference = FakeDocRef(doc_id)
        self._data = data

    def to_dict(self):
        return self._data


class _Snapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeFirestoreNode:
    """Path-tracking Firestore shim: records set/add and serves seeded .get()s."""

    def __init__(self, root, path=None):
        self.root = root
        self.path = path or []

    def collection(self, name):
        return FakeFirestoreNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeFirestoreNode(self.root, self.path + ["document", name])

    def get(self):
        return _Snapshot(self.root.seeded.get(tuple(self.path)))

    def set(self, data, merge=False):
        self.root.set_calls.append((tuple(self.path), data, merge))

    def add(self, data):
        self.root.add_calls.append((tuple(self.path), data))
        return FakeFirestoreNode(self.root, self.path + ["document", "auto-id"])


class FakeFirestore:
    def __init__(self):
        self.set_calls = []
        self.add_calls = []
        self.seeded = {}
        self.seeded[(
            "collection", "systemConfig", "document", "campaignAccess",
        )] = {"automationEnabled": True, "allowedUids": []}

    def seed_client(self, user_id, client_id, client_data):
        path = (
            "collection", "users", "document", user_id,
            "collection", "clients", "document", str(client_id),
        )
        self.seeded[path] = client_data

    def seed_thread(self, user_id, thread_id, thread_data, message_ids=()):
        # #16 pre-send thread-binding validation re-reads the server-side thread
        # (must exist, be open, match the item's clientId) and confirms the
        # replyToMessageId is a recorded message under it. Seed both so a valid
        # dashboard reply passes validation and reaches the send path.
        tpath = (
            "collection", "users", "document", user_id,
            "collection", "threads", "document", str(thread_id),
        )
        self.seeded[tpath] = thread_data
        for mid in message_ids:
            self.seeded[tpath + ("collection", "messages", "document", str(mid))] = {
                "sourceMessage": {"graphMessageId": str(mid)},
            }

    def collection(self, name):
        return FakeFirestoreNode(self, ["collection", name])


# --------------------------------------------------------------------------- #
# Cell: core.stop_cancel_dismiss / bad_placeholder  (state: live_waiting)
# --------------------------------------------------------------------------- #
class StopCancelDismissBadPlaceholderTest(unittest.TestCase):
    """A queued send that STILL carries an unresolved outbound placeholder is
    dismissed by the operator while live-waiting. The real cancel/dismiss gate
    in ``_send_single_outbox_item`` must tear it down BEFORE the placeholder
    dead-letter path -- deleting it, refusing any Graph send (so the raw
    ``[Broker Name]`` never emails a broker), and finalizing the action audit as
    ``cancelled`` (a dismissal), not ``dead_lettered``.
    """

    PLACEHOLDER_SCRIPT = (
        "Hi [Broker Name],\n\nFollowing up on the property at 0 Gemini Ave.\n\nThanks"
    )

    def _placeholder_queued_snapshot(self):
        # A dashboard-reply item (thread anchors force the individual send path)
        # whose body was never personalized -- it still has a live merge field.
        return {
            "assignedEmails": ["broker@example.com"],
            "script": self.PLACEHOLDER_SCRIPT,
            "clientId": "client-1",
            "notificationClientId": "client-1",
            "notificationId": "notification-1",
            "subject": "RE: 0 Gemini Ave, Houston",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "conversationId": "conversation-1",
            "rowNumber": 20,
            "actionAuditId": "audit-badph-1",
            "source": "dashboard_manual_reply",
            "followUpConfig": {"enabled": False},
        }

    def test_dismissed_placeholder_item_is_cancelled_not_dead_lettered_and_never_sends(self):
        queued = self._placeholder_queued_snapshot()

        # Premise guard: the body genuinely bears an unresolved placeholder that
        # the outbound safety layer would otherwise refuse -- so we are proving
        # the DISMISS path preempts the placeholder guard, not a benign body.
        found = find_unresolved_placeholders(queued["script"])
        self.assertIn("[Broker Name]", found)
        self.assertFalse(validate_outbound_body(queued["script"]).is_safe)

        # The operator dismissed this queued item while it was live-waiting.
        dismissed = {**queued, "cancelRequested": True, "status": "cancelled"}
        self.assertTrue(email_module._is_cancelled_outbox_item(dismissed))

        doc = FakeDoc(dismissed, doc_id="outbox-badph-1")
        fake_fs = FakeFirestore()

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_reply_message_sender") as get_reply_sender, \
             patch.object(email_module, "_send_outbox_as_reply") as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email, \
             patch.object(email_module, "_move_to_dead_letter") as move_to_dead_letter:
            email_module._send_single_outbox_item(
                "uid-1",
                {"Authorization": "Bearer token"},
                {"doc": doc, "data": dismissed},
            )

        # No Graph send of any kind -> the raw placeholder body never reaches a broker.
        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()

        # The DISMISS gate preempts the placeholder dead-letter path: the item is
        # deleted and finalized as a cancellation, NOT dead-lettered.
        self.assertTrue(doc.reference.deleted)
        move_to_dead_letter.assert_not_called()
        self.assertTrue(fake_fs.set_calls, "expected a terminal action-audit write")
        audit_payload = fake_fs.set_calls[-1][1]
        self.assertEqual("cancelled", audit_payload["status"])
        self.assertEqual("outbox-badph-1", audit_payload["outboxId"])

    def test_uncancelled_placeholder_item_is_not_torn_down_by_the_dismiss_gate(self):
        # DISCRIMINATING CONTROL: the SAME placeholder-bearing body, but NOT
        # dismissed. The dismiss gate must be keyed on the cancel/dismiss request,
        # not on the placeholder -- so it is a no-op here (the placeholder would be
        # handled downstream by the dead-letter guard, a different feature).
        queued = self._placeholder_queued_snapshot()
        self.assertIn("[Broker Name]", find_unresolved_placeholders(queued["script"]))
        self.assertFalse(email_module._is_cancelled_outbox_item(queued))

        doc = FakeDoc(queued, doc_id="outbox-badph-2")
        fake_fs = FakeFirestore()
        with patch("email_automation.clients._fs", fake_fs):
            tore_down = email_module._delete_cancelled_outbox_item_if_needed(
                doc.reference, queued, user_id="uid-1"
            )

        self.assertFalse(tore_down, "placeholder alone must not trigger the dismiss teardown")
        self.assertFalse(doc.reference.deleted)
        self.assertEqual([], fake_fs.set_calls)


# --------------------------------------------------------------------------- #
# Cell: core.stop_cancel_dismiss / manual_continuation  (state: live_waiting)
# --------------------------------------------------------------------------- #
class StopCancelDismissManualContinuationTest(unittest.TestCase):
    """An operator manually continued a broker thread (a queued dashboard reply
    that was NOT cancelled), then the client was stopped. The cancel/dismiss gate
    is a no-op on this un-cancelled item, yet the INDEPENDENT client-stop gate
    (``_pause_client_outbox_item_if_needed`` -> real ``get_client_automation_pause``)
    must still stop the manual continuation from sending after the stop -- so
    automation never resumes on top of the operator's manual thread. A live-client
    negative control proves the same pipeline DOES send when not stopped.
    """

    CLIENT_ID = "client-manual-cont"

    def _manual_continuation_snapshot(self):
        # source=dashboard_manual_reply + thread anchors: an operator manual
        # continuation of an existing broker thread. No cancel markers.
        return {
            "assignedEmails": ["broker@example.com"],
            "script": "Hi Ron,\n\nThanks -- continuing our conversation below.\n\nBest",
            "clientId": self.CLIENT_ID,
            "notificationClientId": self.CLIENT_ID,
            "notificationId": "notification-1",
            "subject": "RE: 0 Gemini Ave, Houston",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "conversationId": "conversation-1",
            "rowNumber": 20,
            "actionAuditId": "audit-mancont-1",
            "source": "dashboard_manual_reply",
            "followUpConfig": {"enabled": False},
        }

    def _run_send(self, client_data):
        """Drive the REAL _send_single_outbox_item with the real client-stop gate.

        Only Graph/sheet leaf calls and the results-pause gate are faked; the
        client-stop decision runs for real off the seeded client state.
        """
        queued = self._manual_continuation_snapshot()
        doc = FakeDoc(queued, doc_id="outbox-mancont-1")
        fake_fs = FakeFirestore()
        fake_fs.seed_client("uid-1", self.CLIENT_ID, client_data)
        # The thread is open (the CLIENT may be stopped; the thread itself is
        # active) with the reply target recorded, so #16's thread-binding
        # validation passes and the stop/no-stop decision governs the send.
        fake_fs.seed_thread(
            "uid-1", "thread-1",
            {"clientId": self.CLIENT_ID, "status": "active", "rowNumber": 20},
            message_ids=["graph-message-1"],
        )

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_claim_outbox_item", return_value=True), \
             patch.object(email_module, "_get_current_outbox_data", return_value=queued), \
             patch.object(email_module, "_pause_results_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "_should_preflight_sent_items_retry", return_value=False), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={}), \
             patch.object(email_module, "_sent_retry_reconciliation_result", return_value={"sent": False}), \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_finalize_successful_outbox_item"), \
             patch.object(
                 email_module, "_get_reply_message_sender",
                 return_value="broker@example.com",
             ) as get_reply_sender, \
             patch.object(
                 email_module, "_send_outbox_as_reply",
                 return_value={
                     "sent": True,
                     "sentMessageId": "m1",
                     "internetMessageId": "i1",
                     "conversationId": "conversation-1",
                     "toRecipients": ["broker@example.com"],
                     "sentRecipients": ["broker@example.com"],
                 },
             ) as send_outbox_as_reply, \
             patch.object(email_module, "send_and_index_email") as send_and_index_email:
            try:
                email_module._send_single_outbox_item(
                    "uid-1",
                    {"Authorization": "Bearer token"},
                    {"doc": doc, "data": queued},
                )
            except Exception as exc:
                self.fail(
                    "_send_single_outbox_item raised unexpectedly after the stop "
                    f"gate decision: {type(exc).__name__}: {exc}"
                )

        return doc, fake_fs, get_reply_sender, send_outbox_as_reply, send_and_index_email

    def test_stop_halts_uncancelled_manual_continuation_via_independent_stop_gate(self):
        queued = self._manual_continuation_snapshot()

        # Premise guard: this manual continuation is NOT cancelled, so the
        # cancel/dismiss gate would NOT catch it -- proving the *stop* gate,
        # a distinct mechanism, is what halts it.
        self.assertFalse(email_module._is_cancelled_outbox_item(queued))
        self.assertFalse(
            email_module._delete_cancelled_outbox_item_if_needed(
                FakeDoc(queued, "outbox-probe").reference, queued, user_id="uid-1"
            )
        )

        # The client was stopped after the operator's manual continuation.
        doc, fake_fs, get_reply_sender, send_outbox_as_reply, send_and_index_email = (
            self._run_send({"status": "stopped", "statusReason": "client_stopped_by_user"})
        )

        # No send: automation does NOT resume on top of the manual thread.
        get_reply_sender.assert_not_called()
        send_outbox_as_reply.assert_not_called()
        send_and_index_email.assert_not_called()

        # The queued manual continuation was moved to the dead-letter queue,
        # deleted from the outbox, and left visible for manual review.
        self.assertTrue(doc.reference.deleted)
        self.assertTrue(fake_fs.add_calls, "expected a dead-letter write for the stopped item")
        dead_letter_payload = fake_fs.add_calls[-1][1]
        self.assertEqual("dead_lettered", dead_letter_payload["status"])
        self.assertIn("campaign is stopped", dead_letter_payload["failureReason"].lower())

    def test_live_client_manual_continuation_still_sends(self):
        # NEGATIVE CONTROL: identical manual continuation, but the client is LIVE
        # (not stopped). The SAME real pipeline must reach the send path -- proving
        # the halt above is caused by the stop, not by the harness never sending.
        doc, _fs, get_reply_sender, send_outbox_as_reply, _sie = self._run_send(
            {"status": "live", "automationPaused": False}
        )

        self.assertFalse(doc.reference.deleted)
        get_reply_sender.assert_called_once()
        send_outbox_as_reply.assert_called_once()


# --------------------------------------------------------------------------- #
# Cell: core.stop_cancel_dismiss / operator_visible_failure
# (state: dead_letter_visible)
# --------------------------------------------------------------------------- #
class StopCancelDismissOperatorVisibleFailureTest(unittest.TestCase):
    """When a client stop cannot cleanly complete a queued send, the real
    ``_pause_client_outbox_item_if_needed`` -> ``_move_to_dead_letter`` path must
    leave an OPERATOR-VISIBLE failure: a dead-letter document plus a terminal
    action-audit carrying the fields the dashboard renders (status=dead_lettered,
    a human failureReason, the originalDocId, and attempts pinned to the max).
    A live-client negative control writes ZERO dead-letter, proving the visible
    failure is emitted only on a genuine stop-failure -- never spuriously.
    """

    CLIENT_ID = "client-ovf"

    def _queued_snapshot(self):
        return {
            "assignedEmails": ["broker@example.com"],
            "script": "Hi Ron,\n\nFollowing up on the property.\n\nThanks",
            "clientId": self.CLIENT_ID,
            "notificationId": "notification-1",
            "threadId": "thread-1",
            "actionAuditId": "audit-ovf-1",
            "attempts": 1,
            "source": "dashboard_manual_reply",
            "followUpConfig": {"enabled": False},
        }

    def test_stopped_client_surfaces_operator_visible_deadletter_and_audit(self):
        data = self._queued_snapshot()
        doc = FakeDoc(data, doc_id="outbox-ovf-1")
        fake_fs = FakeFirestore()
        fake_fs.seed_client(
            "uid-1",
            self.CLIENT_ID,
            {"status": "stopped", "statusReason": "client_stopped_by_user"},
        )

        with patch("email_automation.clients._fs", fake_fs):
            paused = email_module._pause_client_outbox_item_if_needed(
                "uid-1", doc.reference, data
            )

        # The stop could not cleanly send -> the item is surfaced, not dropped.
        self.assertTrue(paused)
        self.assertTrue(doc.reference.deleted)

        # Operator-visible dead-letter record with the fields the dashboard renders.
        self.assertTrue(fake_fs.add_calls, "expected a dead-letter queue entry")
        dead_letter_path, dead_letter_payload = fake_fs.add_calls[-1]
        self.assertIn("deadLetterQueue", dead_letter_path)
        self.assertEqual("dead_lettered", dead_letter_payload["status"])
        self.assertEqual("outbox-ovf-1", dead_letter_payload["originalDocId"])
        self.assertIn("client_stopped_by_user", dead_letter_payload["failureReason"])
        self.assertEqual(email_module.MAX_OUTBOX_ATTEMPTS, dead_letter_payload["attempts"])

        # Terminal action-audit so the operator sees the blocked action's outcome.
        self.assertTrue(fake_fs.set_calls, "expected a terminal action-audit write")
        audit_path, audit_payload, merge = fake_fs.set_calls[-1]
        self.assertIn("actionAudit", audit_path)
        self.assertTrue(merge)
        self.assertEqual("dead_lettered", audit_payload["status"])
        self.assertEqual("outbox-ovf-1", audit_payload["outboxId"])
        self.assertIn("client_stopped_by_user", audit_payload["failureReason"])

    def test_live_client_emits_no_operator_visible_failure(self):
        # DISCRIMINATING CONTROL: a LIVE client is not stopped, so no failure is
        # surfaced -- the operator-visible dead-letter must NOT be written.
        data = self._queued_snapshot()
        doc = FakeDoc(data, doc_id="outbox-ovf-2")
        fake_fs = FakeFirestore()
        fake_fs.seed_client(
            "uid-1",
            self.CLIENT_ID,
            {"status": "live", "automationPaused": False},
        )

        with patch("email_automation.clients._fs", fake_fs):
            paused = email_module._pause_client_outbox_item_if_needed(
                "uid-1", doc.reference, data
            )

        self.assertFalse(paused)
        self.assertFalse(doc.reference.deleted)
        self.assertEqual([], fake_fs.add_calls)
        self.assertEqual([], fake_fs.set_calls)


if __name__ == "__main__":
    unittest.main()
