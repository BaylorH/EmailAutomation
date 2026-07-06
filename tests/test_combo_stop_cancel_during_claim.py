import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from email_automation import email as email_module


# ---------------------------------------------------------------------------
# Firestore double. Records the two writes the terminal-state guards make:
#   - actionAudit .set(payload, merge=True)  (dashboard terminal state)
#   - deadLetterQueue .add(payload)          (paused/manual-review parking)
# so a test can prove WHICH terminal state a blocked send resolved to.
# ---------------------------------------------------------------------------
class FakeDocRef:
    def __init__(self, doc_id="outbox-1"):
        self.id = doc_id
        self.deleted = False
        self.set_payloads = []

    def delete(self):
        self.deleted = True

    def set(self, payload, merge=False):
        self.set_payloads.append((payload, merge))


class FakeDoc:
    def __init__(self, data, doc_id="outbox-1"):
        self.id = doc_id
        self.reference = FakeDocRef(doc_id)
        self._data = data

    def to_dict(self):
        return self._data


class _FakeSnap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeNode:
    def __init__(self, root, path):
        self.root = root
        self.path = path

    def collection(self, name):
        return FakeNode(self.root, self.path + ["collection", name])

    def document(self, name):
        return FakeNode(self.root, self.path + ["document", name])

    def get(self):
        return _FakeSnap(self.root.seeded.get(tuple(self.path)))

    def set(self, data, merge=False):
        self.root.set_calls.append((tuple(self.path), data, merge))

    def add(self, data):
        self.root.add_calls.append((tuple(self.path), data))
        return FakeNode(self.root, self.path + ["document", "auto-id"])


class FakeFirestore:
    def __init__(self):
        self.set_calls = []
        self.add_calls = []
        self.seeded = {}

    def collection(self, name):
        return FakeNode(self, ["collection", name])

    def seed_thread(self, user_id, thread_id, thread_data, message_ids=()):
        # #16 thread-binding validation re-reads the server-side thread and the
        # recorded reply-target message. Seed both so a valid dashboard reply
        # passes validation and the stop/cancel/continuation guards (not a
        # missing thread) govern the outcome.
        tpath = (
            "collection", "users", "document", user_id,
            "collection", "threads", "document", str(thread_id),
        )
        self.seeded[tpath] = thread_data
        for mid in message_ids:
            self.seeded[tpath + ("collection", "messages", "document", str(mid))] = {
                "sourceMessage": {"graphMessageId": str(mid)},
            }

    # -- convenience views over recorded writes -----------------------------
    def audit_payloads(self):
        return [
            data
            for path, data, _merge in self.set_calls
            if "actionAudit" in path
        ]

    def dead_letter_payloads(self):
        return [
            data
            for path, data in self.add_calls
            if "deadLetterQueue" in path
        ]


class ComboStopCancelDuringClaimTests(unittest.TestCase):
    """combinationStressDeck: stop_cancel_during_claim.

    Chains dashboard_action_resolution (stop / cancel / dismiss) with
    retry_after_uncertain_send and followup_due, across the playbooks
    manual_reply_before_retry, graph_accepted_but_index_missing, and
    row_move_during_pending_action.

    Every case drives the REAL production sender
    email.py::_send_single_outbox_item end to end with only the Firestore /
    Graph / Sheets boundaries faked (ZERO live sends). The deck-level invariant
    is that a cancel/stop/manual-continuation that lands anywhere between the
    scan and the send must block the Graph send on EVERY interleaving, record a
    visible terminal action-audit state, schedule NO stale follow-up, and never
    double-send after a manual continuation -- while a genuinely clean item on
    the SAME code path still sends against a durable row anchor (so the guards
    are proven live, not a vacuous block).
    """

    UID = "uid-1"
    HEADERS = {"Authorization": "Bearer token"}

    # ---- shared fixtures --------------------------------------------------
    def _sendable_reply_snapshot(self, **overrides):
        """A dashboard-approved reply that looks fully sendable on a scan.

        Carries a real recipient + body and NO cancel markers, so a naive run
        would send it. Thread anchors force the individual reply send path.
        """
        base = {
            "assignedEmails": ["broker@example.com"],
            "script": "Hi Ron,\n\nFollowing up on the property.\n\nThanks",
            "clientId": "client-1",
            "notificationClientId": "client-1",
            "notificationId": "notification-1",
            "subject": "RE: 0 Gemini Ave, Houston",
            "threadId": "thread-1",
            "replyToMessageId": "graph-message-1",
            "conversationId": "conversation-1",
            "rowNumber": 20,
            "actionAuditId": "audit-1",
            "source": "dashboard_manual_reply",
            "followUpConfig": {"enabled": False},
        }
        base.update(overrides)
        return base

    @contextmanager
    def _drive(
        self,
        queued,
        *,
        live_data=None,
        claim=True,
        client_paused=(False, "", {}),
        reply_sender="broker@example.com",
        retry_result=None,
        send_reply_result=None,
    ):
        """Run the REAL _send_single_outbox_item with only the boundaries faked.

        Yields (fake_fs, obs) where obs exposes the send mocks + finalize/
        followup observations. `retry_result` is the value the REAL retry
        reconciliation wrapper should resolve to; we inject it through the
        underlying sent_mail_guard lookups so the real wrapper logic runs.
        """
        doc = FakeDoc(dict(queued), doc_id=queued.get("_docId", "outbox-1"))
        fake_fs = FakeFirestore()
        # Seed the open server-side thread + reply target for the reply-anchored
        # snapshots so #16 thread-binding validation passes.
        fake_fs.seed_thread(
            self.UID, "thread-1",
            {"clientId": "client-1", "status": "active", "rowNumber": 20},
            message_ids=["graph-message-1"],
        )
        fresh = live_data if live_data is not None else queued

        retry_result = retry_result or {}
        match = retry_result.get("_match")
        continuation = retry_result.get("_continuation")

        with patch("email_automation.clients._fs", fake_fs), \
             patch.object(email_module, "_pause_results_outbox_item_if_needed", return_value=False), \
             patch.object(email_module, "get_client_automation_pause", return_value=client_paused), \
             patch.object(email_module, "_get_current_outbox_data", return_value=fresh), \
             patch.object(email_module, "_claim_outbox_item", return_value=claim) as claim_mock, \
             patch.object(email_module, "_get_reply_message_sender", return_value=reply_sender), \
             patch.object(email_module, "_fetch_graph_message_metadata", return_value={}), \
             patch.object(email_module, "find_matching_sent_message_for_retry", return_value=match), \
             patch.object(email_module, "find_sent_conversation_continuation_for_retry",
                          return_value=continuation), \
             patch.object(email_module, "_save_outbox_reply_message"), \
             patch.object(email_module, "_finalize_successful_outbox_item") as finalize_mock, \
             patch.object(email_module, "_send_outbox_as_reply",
                          return_value=send_reply_result or {}) as send_reply_mock, \
             patch.object(email_module, "send_and_index_email") as send_index_mock, \
             patch.object(email_module, "_find_row_by_email") as find_row_mock, \
             patch.object(email_module, "_sheets_client") as sheets_mock, \
             patch("email_automation.followup.schedule_followup_for_thread") as followup_mock:
            email_module._send_single_outbox_item(
                self.UID, self.HEADERS, {"doc": doc, "data": dict(queued)}
            )
            obs = {
                "doc": doc,
                "claim_mock": claim_mock,
                "send_reply_mock": send_reply_mock,
                "send_index_mock": send_index_mock,
                "finalize_mock": finalize_mock,
                "followup_mock": followup_mock,
                "find_row_mock": find_row_mock,
                "sheets_mock": sheets_mock,
            }
            yield fake_fs, obs

    def _assert_no_graph_send(self, obs):
        obs["send_reply_mock"].assert_not_called()
        obs["send_index_mock"].assert_not_called()
        obs["finalize_mock"].assert_not_called()

    def _assert_no_stale_followup(self, obs):
        obs["followup_mock"].assert_not_called()

    # ---- Variant 1: cancel BEFORE worker claim ----------------------------
    def test_cancel_before_worker_claim_blocks_send_and_terminalizes(self):
        # The item is already cancelled when the worker picks the snapshot up.
        queued = self._sendable_reply_snapshot(cancelRequested=True, status="cancelled")

        with self._drive(queued, claim=True) as (fake_fs, obs):
            # Pre-claim cancel reconciliation fires: the claim is never attempted.
            obs["claim_mock"].assert_not_called()
            self._assert_no_graph_send(obs)
            self._assert_no_stale_followup(obs)
            self.assertTrue(obs["doc"].reference.deleted, "cancelled item must be removed")

            audits = fake_fs.audit_payloads()
            self.assertTrue(audits, "a terminal action-audit state must be visible")
            self.assertEqual("cancelled", audits[-1]["status"])
            self.assertEqual("outbox-1", audits[-1]["outboxId"])

    # ---- Variant 2: cancel AFTER claim, before send (pre-send recheck) -----
    def test_cancel_after_claim_before_send_is_caught_on_presend_recheck(self):
        queued = self._sendable_reply_snapshot()
        # Premise guard: the scanned snapshot is genuinely sendable, so a hit
        # here proves the LIVE recheck (not a stale terminal state) blocked it.
        self.assertFalse(email_module._is_cancelled_outbox_item(queued))
        live_cancelled = {**queued, "cancelRequested": True, "status": "cancelled"}

        with self._drive(queued, live_data=live_cancelled, claim=True) as (fake_fs, obs):
            obs["claim_mock"].assert_called_once()  # claim succeeded this run
            self._assert_no_graph_send(obs)
            self._assert_no_stale_followup(obs)
            self.assertTrue(obs["doc"].reference.deleted)
            self.assertEqual("cancelled", fake_fs.audit_payloads()[-1]["status"])

    # ---- Variant 3: dismiss/cancel action while a RETRY exists -------------
    def test_cancel_wins_over_pending_retry(self):
        # A retry item (attempts>0, Sent-Items preflight armed) that the operator
        # dismisses/cancels: cancel must win over the retry path -> no send, no
        # reconciliation-as-send, cancelled terminal state.
        queued = self._sendable_reply_snapshot(attempts=2, lastError="prior timeout")
        live_cancelled = {**queued, "status": "cancelling"}  # optimistic UI cancel

        with self._drive(
            queued,
            live_data=live_cancelled,
            # Even if a stale Sent-Items match existed, cancel is checked first.
            retry_result={"_match": {"id": "sent-1"}},
        ) as (fake_fs, obs):
            self._assert_no_graph_send(obs)
            self._assert_no_stale_followup(obs)
            self.assertTrue(obs["doc"].reference.deleted)
            self.assertEqual("cancelled", fake_fs.audit_payloads()[-1]["status"])

    # ---- Variant 4: STOP client while a follow-up is due ------------------
    def test_stop_while_followup_due_blocks_send_and_schedules_no_followup(self):
        # A follow-up-enabled reply whose client the operator stopped mid-claim.
        queued = self._sendable_reply_snapshot(followUpConfig={"enabled": True})

        with self._drive(
            queued,
            client_paused=(True, "client_stopped_by_user", {}),
        ) as (fake_fs, obs):
            self._assert_no_graph_send(obs)
            # The core followup_due invariant: nothing re-arms a follow-up send.
            self._assert_no_stale_followup(obs)
            self.assertTrue(obs["doc"].reference.deleted, "stopped item parks to dead-letter")

            dl = fake_fs.dead_letter_payloads()
            self.assertTrue(dl, "stopped send must park for manual review")
            self.assertIn("client_stopped_by_user", dl[-1]["failureReason"])
            # Terminal action-audit shows dead_lettered, NOT sent.
            self.assertEqual("dead_lettered", fake_fs.audit_payloads()[-1]["status"])

    # ---- Variant 5: manual_reply_before_retry -----------------------------
    def test_manual_continuation_before_retry_suppresses_duplicate_send(self):
        # Retry after an uncertain send; Sent Items shows the user already
        # continued this conversation by hand -> the retry must reconcile, not
        # re-send (no duplicate after manual continuation).
        queued = self._sendable_reply_snapshot(attempts=1, lastError="prior 503")

        with self._drive(
            queued,
            retry_result={
                "_match": None,  # no exact prior-send match ...
                "_continuation": {  # ... but a human continuation exists
                    "sentDateTime": "2026-07-02T12:00:00Z",
                    "recipientCount": 1,
                },
            },
        ) as (fake_fs, obs):
            self._assert_no_graph_send(obs)
            self._assert_no_stale_followup(obs)
            self.assertTrue(obs["doc"].reference.deleted)

            dl = fake_fs.dead_letter_payloads()
            self.assertTrue(dl, "manual continuation must park the stale retry")
            self.assertIn("manually continued", dl[-1]["failureReason"])
            self.assertEqual("dead_lettered", fake_fs.audit_payloads()[-1]["status"])

    # ---- Variant 6: graph_accepted_but_index_missing ----------------------
    def test_retry_reconciles_prior_accepted_send_instead_of_double_sending(self):
        # A prior attempt was accepted by Graph (found in Sent Items). The retry
        # must detect it and NOT send a second copy.
        queued = self._sendable_reply_snapshot(attempts=1, lastError="index write failed")

        with self._drive(
            queued,
            retry_result={"_match": {"id": "sent-prior", "internetMessageId": "<prior@contoso>"}},
        ) as (fake_fs, obs):
            # No second Graph send: the prior accepted send is reconciled.
            obs["send_reply_mock"].assert_not_called()
            obs["send_index_mock"].assert_not_called()
            self._assert_no_stale_followup(obs)
            # Item is not silently dropped: a reconciliation record is written
            # and the original is removed rather than left to re-fire.
            self.assertTrue(obs["doc"].reference.deleted)
            audits = fake_fs.audit_payloads()
            self.assertTrue(audits, "reconciliation must leave a visible audit trail")
            self.assertNotEqual("sent", audits[-1]["status"])

    # ---- Variant 7: placeholder body never reaches a send -----------------
    def test_placeholder_reply_body_is_blocked_before_send(self):
        # A dashboard reply whose body still carries an unresolved [NAME] token
        # must be parked, not sent -- proven on the same real send path.
        queued = self._sendable_reply_snapshot(script="Hi [NAME],\n\nFollowing up.\n\nThanks")

        with self._drive(queued) as (fake_fs, obs):
            self._assert_no_graph_send(obs)
            self._assert_no_stale_followup(obs)
            self.assertTrue(obs["doc"].reference.deleted)
            self.assertEqual("dead_lettered", fake_fs.audit_payloads()[-1]["status"])

    # ---- POSITIVE CONTROL: a clean item DOES send, on the durable anchor ---
    def test_clean_reply_sends_on_durable_row_anchor(self):
        # No cancel, live client, first attempt, matching reply sender, clean
        # body, no manual continuation -> the SAME code path sends. This proves
        # every negative above is a live guard firing, not a dead send path,
        # and that the send uses the durable rowNumber anchor (row_move safe):
        # no email->row sheet lookup is performed.
        queued = self._sendable_reply_snapshot()
        sent = {
            "sent": True,
            "sentMessageId": "graph-new-1",
            "internetMessageId": "<new@contoso>",
            "conversationId": "conversation-1",
            "toRecipients": ["broker@example.com"],
            "sentRecipients": ["broker@example.com"],
        }

        with self._drive(queued, send_reply_result=sent) as (fake_fs, obs):
            obs["send_reply_mock"].assert_called_once()
            obs["send_index_mock"].assert_not_called()
            obs["finalize_mock"].assert_called_once()
            # Durable anchor: finalize uses the item's rowNumber, and no
            # email->row lookup (which could hit a MOVED row) was performed.
            _args, kwargs = obs["finalize_mock"].call_args
            self.assertEqual(20, kwargs.get("row_number"))
            obs["find_row_mock"].assert_not_called()
            obs["sheets_mock"].assert_not_called()
            self.assertFalse(obs["doc"].reference.deleted,
                             "finalize owns deletion; the handler must not double-delete")


if __name__ == "__main__":
    unittest.main()
