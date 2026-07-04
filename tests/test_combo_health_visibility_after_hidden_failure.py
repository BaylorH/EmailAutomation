"""Combination stress deck: health_visibility_after_hidden_failure.

Deck (docs/release-safety/feature-gradebook.json ->
combinationStressDecks.health_visibility_after_hidden_failure) chains three
playbooks around a single broker conversation:

  1. graph_accepted_but_index_missing  - Graph accepted the send but the
     downstream index/actionAudit write failed. The send LIKELY went out but the
     system could not prove it, so the item is surfaced as a needs_reconciliation
     dead-letter (a HIDDEN failure that must NOT stay hidden).
  2. manual_reply_before_retry         - before any autonomous retry fires, the
     user manually continues the same thread. The retry must be suppressed, not
     double-sent.
  3. row_move_during_pending_action    - while the action was pending, the sheet
     rows shifted. The durable rowNumber anchor must catch that the queued
     recipient no longer matches the row and block a wrong-recipient send.

mustProve (from the deck):
  - health/recovery views show failure
  - retry guard checks Sent Items before sending
  - operator can cancel or recover without double-send

This test drives the REAL handlers end-to-end across the interaction, faking
only the Firestore and Microsoft Graph boundaries. There are NO live sends, no
live sheet writes, no live Firestore. Every safety invariant is asserted on
concrete resulting state, and every positive assertion is paired with a negative
control so the test genuinely FAILS if any guard in the chain regresses.

Real handlers exercised (each owns one leg of the interaction):
  email._record_outbox_reconciliation            (hidden failure -> dead-letter)
  system_health.collect_user_health              (health view surfaces failure)
  email._sent_retry_reconciliation_result        (retry guard reads Sent Items)
  email._dead_letter_campaign_recipient_row_...  (durable row anchor guard)
  dead_letter_recovery.resolve_dead_letter_item  (operator recover w/o double-send)
"""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import email as email_mod
from email_automation import system_health
from email_automation import sent_mail_guard
from email_automation import dead_letter_recovery


BROKER = "broker@acme-realty.com"
CONV = "AAQkConversation-777"
SENT_AFTER = datetime(2026, 7, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fake Microsoft Graph boundary (Sent Items reads only).
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, value, status_code=200):
        self.status_code = status_code
        self.headers = {}
        self._payload = {"value": value}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def make_sent_message(*, body_html, subject, to=(), conversation_id=None, sent="2026-07-02T18:00:00Z"):
    return {
        "id": "AAMk-sent-001",
        "internetMessageId": "<sent@contoso.com>",
        "conversationId": conversation_id,
        "subject": subject,
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [],
        "bccRecipients": [],
        "sentDateTime": sent,
        "body": {"contentType": "html", "content": body_html},
        "bodyPreview": "",
    }


# --------------------------------------------------------------------------- #
# Unified in-memory Firestore double. Supports BOTH the write path used by the
# failure/recovery handlers (add/update/set/delete/.id) AND the read path used
# by the health view (limit/stream/to_dict/.exists), over one shared store so a
# failure written by one feature is visible to another.
# --------------------------------------------------------------------------- #
class FakeSnapshot:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeDocRef:
    def __init__(self, store, path):
        self._store = store
        self._path = path
        self.id = path[-1]

    @property
    def reference(self):
        return self

    def get(self):
        return FakeSnapshot(self.id, self._store.get(self._path))

    def collection(self, name):
        return FakeCollection(self._store, self._path + (name,))

    def update(self, payload):
        self._store.setdefault(self._path, {}).update(payload)

    def set(self, payload, merge=False):
        if merge:
            self._store.setdefault(self._path, {}).update(payload)
        else:
            self._store[self._path] = dict(payload)

    def delete(self):
        self._store.pop(self._path, None)


class FakeCollection:
    def __init__(self, store, prefix):
        self._store = store
        self._prefix = prefix
        self._auto = 0

    def document(self, doc_id):
        return FakeDocRef(self._store, self._prefix + (doc_id,))

    def collection(self, name):
        return FakeCollection(self._store, self._prefix + (name,))

    def add(self, payload):
        self._auto += 1
        doc_id = f"auto-{len(self._store)}-{self._auto}"
        ref = FakeDocRef(self._store, self._prefix + (doc_id,))
        self._store[self._prefix + (doc_id,)] = dict(payload)
        return (None, ref)

    def limit(self, n):
        return self

    def order_by(self, *a, **k):
        return self

    def stream(self):
        n = len(self._prefix)
        out = []
        for key, data in list(self._store.items()):
            if len(key) == n + 1 and key[:n] == self._prefix:
                out.append(FakeSnapshot(key[-1], data))
        return out


class FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCollection(self.store, (name,))

    # convenience for assertions
    def dead_letters(self, user_id):
        return [
            data
            for key, data in self.store.items()
            if key[:3] == ("users", user_id, "deadLetterQueue") and len(key) == 4
        ]

    def outbox(self, user_id):
        return [
            data
            for key, data in self.store.items()
            if key[:3] == ("users", user_id, "outbox") and len(key) == 4
        ]


class ComboHealthVisibilityAfterHiddenFailure(unittest.TestCase):
    USER = "user-1"

    # ------------------------------------------------------------------ #
    # helpers that drive the REAL guards against a faked Graph
    # ------------------------------------------------------------------ #
    def _reconciliation(self, data, *, recipient, body, subject, conversation_id, sent_messages):
        with mock.patch.object(sent_mail_guard.requests, "get", return_value=FakeResponse(sent_messages)):
            return email_mod._sent_retry_reconciliation_result(
                {"Authorization": "Bearer t"},
                data,
                recipient,
                body,
                subject,
                conversation_id=conversation_id,
            )

    def _resolve(self, fake_fs, dead_letter_id, *, action, sent_messages):
        with mock.patch("email_automation.clients._fs", fake_fs, create=True), \
             mock.patch.object(sent_mail_guard.requests, "get", return_value=FakeResponse(sent_messages)):
            return dead_letter_recovery.resolve_dead_letter_item(
                self.USER,
                dead_letter_id,
                action=action,
                headers={"Authorization": "Bearer t"},
                operator_id="op-1",
            )

    # ================================================================== #
    # LEG 1 - graph_accepted_but_index_missing: the hidden failure must
    # be surfaced in the health/recovery view (mustProve #1).
    # ================================================================== #
    def test_hidden_failure_is_surfaced_in_health_view(self):
        fake_fs = FakeFirestore()

        # Baseline: a clean user is HEALTHY with zero dead-letters. This is the
        # negative control - it proves the "warning" below is the failure being
        # detected, not a view that is stuck warning unconditionally.
        clean = system_health.collect_user_health(
            self.USER, fs_client=fake_fs,
            token_state={"status": "healthy"}, graph_state={"status": "healthy"},
        )
        self.assertEqual("healthy", clean["status"])
        self.assertEqual(0, clean["queues"]["deadLetterQueue"])

        # Graph accepted the send but the identity/index lookup came back empty:
        # the REAL handler records a needs_reconciliation dead-letter instead of
        # silently dropping it or retrying (which would risk a double send).
        outbox_doc = fake_fs.collection("users").document(self.USER).collection("outbox").document("ob-1")
        data = {
            "source": "outbox",
            "assignedEmails": [BROKER],
            "script": "Following up on 404 New Way; are you available for a tour Thursday?",
            "subject": "404 New Way",
            "conversationId": CONV,
            "actionAuditId": "aa-1",
            "clientId": "client-1",
        }
        with mock.patch("email_automation.clients._fs", fake_fs, create=True):
            email_mod._record_outbox_reconciliation(
                self.USER,
                outbox_doc,
                data,
                "Graph accepted send but Sent Items identity lookup failed; operator reconciliation required",
                {"sent": [BROKER]},
                [BROKER],
                delete_original=True,
            )

        # The failure is now visible: a needs_reconciliation / alreadySent item
        # exists and the health view rolls it up to a warning.
        dls = fake_fs.dead_letters(self.USER)
        self.assertEqual(1, len(dls))
        self.assertTrue(dls[0].get("alreadySent"))
        self.assertEqual("needs_reconciliation", dls[0].get("status"))

        after = system_health.collect_user_health(
            self.USER, fs_client=fake_fs,
            token_state={"status": "healthy"}, graph_state={"status": "healthy"},
        )
        self.assertEqual("warning", after["status"],
                         "Hidden (graph-accepted, index-missing) failure must surface in the health view.")
        self.assertEqual(1, after["queues"]["deadLetterQueue"])

    # ================================================================== #
    # LEG 2 - retry guard checks Sent Items BEFORE sending (mustProve #2).
    # The same uncertain send is now retried. The guard must reconcile
    # Sent Items first: detect the already-sent copy OR a manual human
    # continuation, and in neither case re-send.
    # ================================================================== #
    def test_retry_guard_reads_sent_items_before_resending(self):
        body = "Following up on 404 New Way; are you available for a tour Thursday?"
        data = {
            "attempts": 1,  # a retry -> preflight is required
            "conversationId": CONV,
            "lastError": "Network timeout after send",
        }

        # (a) already-sent copy sits in Sent Items -> guard returns a `sent`
        # identity so the retry path reconciles instead of sending again.
        already_sent = [make_sent_message(body_html=body, subject="RE: 404 New Way",
                                           to=[BROKER], conversation_id=CONV)]
        r_sent = self._reconciliation(data, recipient=BROKER, body=body,
                                      subject="404 New Way", conversation_id=CONV,
                                      sent_messages=already_sent)
        self.assertIn(BROKER, r_sent.get("sent", []),
                      "Retry guard must detect the already-sent copy and block a double send.")
        self.assertNotIn("manualContinuation", r_sent)

        # (b) NEGATIVE CONTROL: Sent Items is empty for this conversation ->
        # guard is silent -> the retry path is free to send. Proves the block in
        # (a)/(c) is real detection, not an unconditional stop.
        r_none = self._reconciliation(data, recipient=BROKER, body=body,
                                      subject="404 New Way", conversation_id=CONV,
                                      sent_messages=[])
        self.assertEqual({}, r_none)

    # ================================================================== #
    # LEG 3 - manual_reply_before_retry: the user continued the thread
    # before the retry. The retry guard's continuation check must flag it
    # so the autonomous send is suppressed.
    # ================================================================== #
    def test_manual_continuation_suppresses_autonomous_retry(self):
        our_body = "Following up on 404 New Way; are you available for a tour Thursday?"
        data = {"attempts": 1, "conversationId": CONV, "lastError": "timeout"}

        # A DIFFERENT, newer message in the SAME conversation = the human's manual
        # reply. It does not body-match our stale draft, so the exact-match guard
        # stays silent, but the conversation-continuation guard fires.
        human_reply = [make_sent_message(
            body_html="Actually let's target next week, I'll send times.",
            subject="RE: 404 New Way", to=[BROKER], conversation_id=CONV,
            sent="2026-07-03T09:00:00Z",
        )]
        r = self._reconciliation(data, recipient=BROKER, body=our_body,
                                 subject="404 New Way", conversation_id=CONV,
                                 sent_messages=human_reply)
        self.assertNotIn("sent", r, "A non-matching human reply must not be misread as our own send.")
        self.assertIn("manualContinuation", r,
                      "A newer human continuation must suppress the autonomous retry.")
        self.assertEqual(1, r["manualContinuation"]["recipientCount"])

        # NEGATIVE CONTROL: the human reply lives in a DIFFERENT conversation ->
        # it must NOT suppress this thread's retry.
        other_conv = [make_sent_message(
            body_html="Different deal entirely.", subject="RE: Other",
            to=[BROKER], conversation_id="OTHER-CONV", sent="2026-07-03T09:00:00Z",
        )]
        r_other = self._reconciliation(data, recipient=BROKER, body=our_body,
                                       subject="404 New Way", conversation_id=CONV,
                                       sent_messages=other_conv)
        self.assertEqual({}, r_other)

    # ================================================================== #
    # LEG 4 - row_move_during_pending_action: durable rowNumber anchor.
    # While the send was pending the sheet rows moved, so the queued
    # recipient no longer sits on rowNumber. The guard must block the
    # wrong-recipient send; if the row still matches it must proceed.
    # ================================================================== #
    def test_row_anchor_blocks_wrong_recipient_after_rows_move(self):
        base = {
            "source": "dashboard_new_campaign",  # campaign launch outbox
            "clientId": "client-1",
            "rowNumber": 4,
            "assignedEmails": [BROKER],
            "actionAuditId": "aa-row",
        }
        header = ["Property Address", "City", "Email"]

        # (a) Rows moved: row 4 now belongs to a DIFFERENT broker. The queued
        # recipient (BROKER) is no longer on the anchored row -> dead-letter,
        # no send to the wrong party.
        fake_fs = FakeFirestore()
        moved_doc = fake_fs.collection("users").document(self.USER).collection("outbox").document("ob-row")
        moved_row = ["500 Market St", "Dallas", "someone-else@other.com"]
        with mock.patch("email_automation.clients._fs", fake_fs, create=True), \
             mock.patch("email_automation.email._campaign_sheet_header_and_row",
                        return_value=(header, moved_row)):
            blocked = email_mod._dead_letter_campaign_recipient_row_mismatch_if_needed(
                self.USER, moved_doc, dict(base), BROKER,
            )
        self.assertTrue(blocked, "Row move must block a send to the recipient no longer on the anchored row.")
        dls = fake_fs.dead_letters(self.USER)
        self.assertEqual(1, len(dls))
        self.assertIn("does not match sheet row 4", dls[0]["failureReason"])

        # (b) NEGATIVE CONTROL: the anchored row still carries the queued
        # recipient -> the guard passes and no dead-letter is written.
        fake_ok = FakeFirestore()
        ok_doc = fake_ok.collection("users").document(self.USER).collection("outbox").document("ob-row")
        good_row = ["404 New Way", "Dallas", BROKER]
        with mock.patch("email_automation.clients._fs", fake_ok, create=True), \
             mock.patch("email_automation.email._campaign_sheet_header_and_row",
                        return_value=(header, good_row)):
            blocked_ok = email_mod._dead_letter_campaign_recipient_row_mismatch_if_needed(
                self.USER, ok_doc, dict(base), BROKER,
            )
        self.assertFalse(blocked_ok, "A row that still matches the queued recipient must NOT be blocked.")
        self.assertEqual(0, len(fake_ok.dead_letters(self.USER)))

    # ================================================================== #
    # LEG 5 - dashboard_action_resolution: operator can recover/cancel a
    # dead-letter without a double send (mustProve #3). Covers the two
    # unsafe requeue paths (already-sent, manual continuation) that must be
    # blocked, the genuinely-unsent path that IS allowed to requeue, and a
    # cancel (discard) that clears the health warning.
    # ================================================================== #
    def _seed_dead_letter(self, fake_fs, dl_id, extra):
        payload = {
            "source": "outbox",
            "assignedEmails": [BROKER],
            "sentRecipients": [BROKER],
            "script": "Following up on 404 New Way; are you available for a tour Thursday?",
            "subject": "404 New Way",
            "conversationId": CONV,
            "status": "dead_lettered",
            "failureReason": "Network timeout after send",
            "actionAuditId": "aa-dl",
        }
        payload.update(extra)
        fake_fs.store[("users", self.USER, "deadLetterQueue", dl_id)] = payload

    def test_operator_recovery_never_double_sends(self):
        body = "Following up on 404 New Way; are you available for a tour Thursday?"

        # (a) already-sent copy in Sent Items -> operator requeue is REFUSED.
        fs_a = FakeFirestore()
        self._seed_dead_letter(fs_a, "dl-a", {})
        already = [make_sent_message(body_html=body, subject="RE: 404 New Way",
                                     to=[BROKER], conversation_id=CONV)]
        res_a = self._resolve(fs_a, "dl-a", action="requeue_verified_unsent", sent_messages=already)
        self.assertFalse(res_a["success"])
        self.assertEqual("already_sent", res_a["code"])
        self.assertEqual(0, len(fs_a.outbox(self.USER)),
                         "Already-sent dead-letter must NOT be requeued into the outbox.")

        # (b) manual human continuation in Sent Items -> operator requeue REFUSED.
        fs_b = FakeFirestore()
        self._seed_dead_letter(fs_b, "dl-b", {})
        human = [make_sent_message(body_html="Let's do next week instead.",
                                   subject="RE: 404 New Way", to=[BROKER],
                                   conversation_id=CONV, sent="2026-07-03T09:00:00Z")]
        res_b = self._resolve(fs_b, "dl-b", action="requeue_verified_unsent", sent_messages=human)
        self.assertFalse(res_b["success"])
        self.assertEqual("blocked_manual_continuation", res_b["code"])
        self.assertEqual(0, len(fs_b.outbox(self.USER)))

        # (c) NEGATIVE CONTROL: genuinely never sent, no continuation -> requeue
        # IS allowed. Proves (a)/(b) are real guards, not an unconditional block.
        fs_c = FakeFirestore()
        self._seed_dead_letter(fs_c, "dl-c", {})
        res_c = self._resolve(fs_c, "dl-c", action="requeue_verified_unsent", sent_messages=[])
        self.assertTrue(res_c["success"])
        self.assertEqual("requeued", res_c["code"])
        outbox = fs_c.outbox(self.USER)
        self.assertEqual(1, len(outbox), "A verified-unsent item must requeue exactly one outbox send.")
        self.assertTrue(outbox[0].get("requiresSentItemsPreflight"),
                        "Requeued item must carry the Sent Items preflight flag so the next attempt re-checks.")

        # (d) operator CANCELS via discard -> no send, and the health view drops
        # back out of warning once the only dead-letter is resolved.
        fs_d = FakeFirestore()
        self._seed_dead_letter(fs_d, "dl-d", {})
        warn = system_health.collect_user_health(
            self.USER, fs_client=fs_d,
            token_state={"status": "healthy"}, graph_state={"status": "healthy"})
        self.assertEqual("warning", warn["status"])
        self.assertEqual(1, warn["queues"]["deadLetterQueue"])

        res_d = self._resolve(fs_d, "dl-d", action="discard", sent_messages=[])
        self.assertTrue(res_d["success"])
        self.assertEqual("discarded", res_d["code"])
        self.assertEqual(0, len(fs_d.outbox(self.USER)), "Cancel must not enqueue any send.")

        healed = system_health.collect_user_health(
            self.USER, fs_client=fs_d,
            token_state={"status": "healthy"}, graph_state={"status": "healthy"})
        self.assertEqual(0, healed["queues"]["deadLetterQueue"],
                         "A resolved (discarded) dead-letter must clear from the active health count.")
        self.assertEqual("healthy", healed["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
