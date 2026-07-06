"""crossFeature: reply_all_privacy_boundary.

Interaction group (Base-V1): core.reply_all_cc + core.manual_reply (manual
continuation) + core.inbox_auto_reply + core.followups.

The invariant that ties these features together: a reply-all thread that carries
Cc context must NEVER

  1. leak the operator's own mailbox back into the reply-all audience (self-send
     loop / operator address disclosed to the broker's Cc list),
  2. echo a blocked / opted-out contact that happens to sit on the inherited Cc
     line (even when that contact is reached via a plus-alias), or
  3. resurrect a thread the human already manually continued into a *duplicate*
     reply-all send.

These three break-vectors live in TWO different real code units that the real
outbox reply path composes IN ORDER (email.py `_process_single_outbox_item`,
lines ~2930-2968):

    prior = _sent_retry_reconciliation_result(...)   # manual-continuation GUARD
    if prior.get("manualContinuation"): move_to_dead_letter(); return   # (3)
    else: _send_outbox_as_reply(...)   # reply-all FILTER strips (1) and (2)

This test drives BOTH real units together, wired in that exact real order, with
faked Microsoft Graph transport and faked Firestore. ZERO live sends: every
Graph POST/PATCH is intercepted and asserted, never dispatched.

If the manual-continuation guard stops detecting a human continuation, the
`suppress` branch collapses and the reply-all send fires -> duplicate
resurrection -> this test fails. If the reply-all filter stops stripping the
operator or an opted-out Cc contact, the sent audience leaks -> this test fails.
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import hashlib
import unittest
from unittest.mock import patch

import email_automation.email as email_mod
import email_automation.sent_mail_guard as guard_mod
import email_automation.processing as processing_mod
from email_automation.email import (
    _send_outbox_as_reply,
    _sent_retry_reconciliation_result,
)


# ---------------------------------------------------------------------------
# Scenario constants
# ---------------------------------------------------------------------------
USER_ID = "user-cre-1"
OPERATOR_EMAIL = "operator@sitesift.com"
OPERATOR_ALIAS = "operator+campaign1@sitesift.com"      # same mailbox as operator
BROKER = "broker@brokerage.com"                          # legit lead (To)
EXTERNAL_CC = "assistant@brokerage.com"                  # legit external Cc context
OPTOUT_BARE = "partner@optout-lead.com"                  # opted out (stored bare)
OPTOUT_ALIAS = "partner+leasing@optout-lead.com"         # SAME mailbox via plus-alias

CONVERSATION_ID = "AAQkConv_reply_all_privacy_1"
REPLY_TO_MSG_ID = "srcmsg-1"
DRAFT_ID = "draft-1"
QUEUED_SCRIPT = "Following up on 404 Main St - are you available for a tour this week?"


def _hash_email(email: str) -> str:
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Fake HTTP transport
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _rq
            raise _rq.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


class _FakeGraphForReplyAll:
    """Fake Microsoft Graph for the real `_send_outbox_as_reply` path.

    Serves createReplyAll + hydrate + records the PATCH/SEND so the test can
    inspect exactly which audience would have gone on the wire. Nothing is ever
    sent to the network.
    """

    def __init__(self, reply_all_audience):
        self._audience = reply_all_audience
        self.patched_payloads = []
        self.send_posts = []

    # --- GET: metadata fetch + draft hydrate + sent-reply identity ---
    def get(self, url, headers=None, params=None, timeout=None):
        if url.endswith(f"/me/messages/{REPLY_TO_MSG_ID}"):
            return _FakeResp(200, {
                "conversationId": CONVERSATION_ID,
                "subject": "404 Main St",
            })
        if url.endswith(f"/me/messages/{DRAFT_ID}"):
            # Graph's computed reply-all audience (hydrate step).
            return _FakeResp(200, {
                "id": DRAFT_ID,
                "toRecipients": self._audience["toRecipients"],
                "ccRecipients": self._audience["ccRecipients"],
            })
        if url.endswith("/me/mailFolders/SentItems/messages"):
            # _find_recent_sent_reply_identity after the send.
            return _FakeResp(200, {"value": [{
                "id": "sent-final-1",
                "internetMessageId": "<sent-final-1>",
                "conversationId": CONVERSATION_ID,
                "subject": "RE: 404 Main St",
                "sentDateTime": "2026-07-04T12:00:00Z",
            }]})
        return _FakeResp(200, {})

    def post(self, url, headers=None, params=None, json=None, timeout=None, data=None):
        if url.endswith(f"/me/messages/{REPLY_TO_MSG_ID}/createReplyAll"):
            return _FakeResp(201, {"id": DRAFT_ID})
        if url.endswith(f"/me/messages/{DRAFT_ID}/send"):
            self.send_posts.append(url)
            return _FakeResp(202, {})
        return _FakeResp(200, {})

    def patch(self, url, headers=None, params=None, json=None, timeout=None, data=None):
        if url.endswith(f"/me/messages/{DRAFT_ID}"):
            self.patched_payloads.append(json)
            return _FakeResp(202, {})
        return _FakeResp(200, {})

    def delete(self, url, headers=None, params=None, timeout=None):
        return _FakeResp(204, {})


class _FakeGraphForGuard:
    """Fake Graph for the real manual-continuation guard (Sent Items lookup)."""

    def __init__(self, sent_items_value):
        self._value = sent_items_value

    def get(self, url, headers=None, params=None, timeout=None):
        if "/me/mailFolders/SentItems/messages" in url:
            return _FakeResp(200, {"value": list(self._value)})
        return _FakeResp(200, {})


# ---------------------------------------------------------------------------
# Fake Firestore for opt-out lookups (drives the REAL is_contact_opted_out)
# ---------------------------------------------------------------------------
class _FakeDoc:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, data):
        self._data = data

    def get(self):
        return _FakeDoc(self._data)


class _FakeOptOutCollection:
    def __init__(self, records_by_hash):
        self._records = records_by_hash

    def document(self, doc_id):
        return _FakeDocRef(self._records.get(doc_id))


class _FakeUserDoc:
    def __init__(self, records_by_hash):
        self._records = records_by_hash

    def collection(self, name):
        if name == "optedOutContacts":
            return _FakeOptOutCollection(self._records)
        return _FakeOptOutCollection({})


class _FakeUsersCollection:
    def __init__(self, records_by_hash):
        self._records = records_by_hash

    def document(self, user_id):
        return _FakeUserDoc(self._records)


class _FakeFirestore:
    def __init__(self, opted_out_emails):
        self._records = {
            _hash_email(email): {"reason": "manual_optout", "email": email}
            for email in opted_out_emails
        }

    def collection(self, name):
        if name == "users":
            return _FakeUsersCollection(self._records)
        return _FakeUsersCollection({})


# ---------------------------------------------------------------------------
# Test-side wiring of the two real units, in the REAL outbox reply order.
# Mirrors email.py `_process_single_outbox_item` (is_thread_reply / use_graph
# branch, lines ~2930-2968) but calls the real functions so the interaction is
# exercised, not stubbed.
# ---------------------------------------------------------------------------
def _outbox_reply_decision(*, guard_graph, send_graph, fake_fs, data):
    """Return ("suppressed", None) or ("sent", res) driving both real units."""
    headers = {"Authorization": "Bearer test"}
    with patch.object(guard_mod, "requests", guard_graph):
        prior = _sent_retry_reconciliation_result(
            headers,
            data,
            BROKER,               # recipient
            QUEUED_SCRIPT,        # body
            "404 Main St",        # subject
            conversation_id=CONVERSATION_ID,
        )
    if prior.get("manualContinuation"):
        return "suppressed", prior
    if prior.get("guardLookupError"):
        return "guard_error", prior
    if prior.get("sent"):
        return "reconciled", prior

    # No continuation -> the real reply-all send fires, real filter inside.
    with patch.object(email_mod, "requests", send_graph), \
            patch.object(processing_mod, "_fs", fake_fs):
        res = _send_outbox_as_reply(
            USER_ID,
            headers,
            QUEUED_SCRIPT,
            REPLY_TO_MSG_ID,
            "thread-1",
            user_email=OPERATOR_EMAIL,
        )
    return "sent", res


def _default_audience():
    return {
        "toRecipients": [
            {"emailAddress": {"address": BROKER, "name": "Broker"}},
        ],
        "ccRecipients": [
            {"emailAddress": {"address": OPERATOR_EMAIL}},        # operator self
            {"emailAddress": {"address": OPERATOR_ALIAS}},        # operator alias
            {"emailAddress": {"address": OPTOUT_ALIAS}},          # opted out (alias)
            {"emailAddress": {"address": EXTERNAL_CC}},           # legit external Cc
            {"emailAddress": {"address": BROKER}},                # duplicate of To
        ],
    }


class ReplyAllPrivacyBoundaryCrossFeatureTests(unittest.TestCase):
    def setUp(self):
        # Reset the module-global operator memo so tests don't cross-contaminate.
        email_mod._LAST_KNOWN_OPERATOR_EMAIL = None
        # Retry posture so the Sent-Items preflight (the guard) actually runs.
        self.retry_data = {
            "attempts": 1,
            "lastSendAttemptAt": "2026-07-04T10:00:00Z",
            "conversationId": CONVERSATION_ID,
        }
        self.fake_fs = _FakeFirestore([OPTOUT_BARE])

    # -- Continuation-present Sent Items: a human already replied in-thread. --
    def _human_continuation_sent_items(self):
        return [{
            "id": "human-1",
            "internetMessageId": "<human-1>",
            "conversationId": CONVERSATION_ID,
            "subject": "RE: 404 Main St",
            # Newer than sent_after (lastSendAttemptAt - 30s), and NO body field
            # so the exact-send matcher can't claim this is our draft already
            # sent -> it is correctly classified as a manual continuation.
            "sentDateTime": "2026-07-04T11:00:00Z",
            "toRecipients": [{"emailAddress": {"address": BROKER}}],
        }]

    # ------------------------------------------------------------------
    # (3) Manual continuation must SUPPRESS the reply-all send entirely.
    #     No resurrection -> and therefore no audience on the wire at all.
    # ------------------------------------------------------------------
    def test_manual_continuation_suppresses_duplicate_reply_all_send(self):
        guard_graph = _FakeGraphForGuard(self._human_continuation_sent_items())
        send_graph = _FakeGraphForReplyAll(_default_audience())

        outcome, prior = _outbox_reply_decision(
            guard_graph=guard_graph,
            send_graph=send_graph,
            fake_fs=self.fake_fs,
            data=self.retry_data,
        )

        self.assertEqual(outcome, "suppressed",
                         "manual continuation must stop the queued reply-all retry")
        self.assertTrue(prior.get("manualContinuation"),
                        "real guard must report the human continuation")
        # The reply-all send path must never have been reached: zero PATCH, zero
        # /send POST -> no duplicate, and (transitively) no operator/opt-out leak.
        self.assertEqual(send_graph.send_posts, [],
                         "a manually-continued thread must not be reply-all resurrected")
        self.assertEqual(send_graph.patched_payloads, [])

    # ------------------------------------------------------------------
    # (1)+(2) When NOT continued, the reply-all send fires but the filter
    #         strips the operator (self + alias), the opted-out Cc contact
    #         (via plus-alias) and the duplicate, keeping legit audience.
    # ------------------------------------------------------------------
    def test_reply_all_send_strips_operator_and_optout_from_cc_audience(self):
        guard_graph = _FakeGraphForGuard([])   # no continuation
        send_graph = _FakeGraphForReplyAll(_default_audience())

        outcome, res = _outbox_reply_decision(
            guard_graph=guard_graph,
            send_graph=send_graph,
            fake_fs=self.fake_fs,
            data=self.retry_data,
        )

        self.assertEqual(outcome, "sent")
        self.assertTrue(res.get("sent"))
        self.assertEqual(len(send_graph.send_posts), 1,
                         "exactly one reply-all send when not continued")

        # What actually went on the wire = the PATCHed audience.
        self.assertEqual(len(send_graph.patched_payloads), 1)
        wire = send_graph.patched_payloads[0]
        wire_to = {r["emailAddress"]["address"].lower() for r in wire["toRecipients"]}
        wire_cc = {r["emailAddress"]["address"].lower() for r in wire["ccRecipients"]}
        wire_all = wire_to | wire_cc

        # (1) operator (and its plus-alias) never leaks back into the audience.
        self.assertNotIn(OPERATOR_EMAIL, wire_all)
        self.assertNotIn(OPERATOR_ALIAS, wire_all)
        # (2) opted-out contact, reached via a plus-alias, is stripped.
        self.assertNotIn(OPTOUT_ALIAS, wire_all)
        self.assertNotIn(OPTOUT_BARE, wire_all)
        # Legit audience preserved.
        self.assertIn(BROKER, wire_to)
        self.assertIn(EXTERNAL_CC, wire_cc)
        # Duplicate broker collapsed (not echoed onto Cc).
        self.assertNotIn(BROKER, wire_cc)

        # The handler's own accounting agrees with the wire.
        sent_recipients = {a.lower() for a in (res.get("sentRecipients") or [])}
        self.assertEqual(sent_recipients, {BROKER, EXTERNAL_CC})
        skipped = res.get("skippedRecipients") or {}
        skipped_operator = {a.lower() for a in skipped.get("operator", [])}
        skipped_optout = {
            (entry.get("email") if isinstance(entry, dict) else entry).lower()
            for entry in skipped.get("optedOut", [])
        }
        self.assertIn(OPERATOR_EMAIL, skipped_operator)
        self.assertIn(OPERATOR_ALIAS, skipped_operator)
        self.assertIn(OPTOUT_ALIAS, skipped_optout)

    # ------------------------------------------------------------------
    # Cross-feature knot: the SAME privacy-laden audience yields opposite
    # correct outcomes depending ONLY on the manual-continuation signal.
    # Proves the two features are genuinely composed, not independent.
    # ------------------------------------------------------------------
    def test_boundary_holds_on_both_axes_from_one_audience(self):
        audience = _default_audience()

        # Axis A: continuation present -> nothing sent.
        g_supp = _FakeGraphForGuard(self._human_continuation_sent_items())
        s_supp = _FakeGraphForReplyAll(audience)
        outcome_a, _ = _outbox_reply_decision(
            guard_graph=g_supp, send_graph=s_supp,
            fake_fs=self.fake_fs, data=self.retry_data,
        )

        # Axis B: no continuation -> sent, but privacy-filtered.
        g_send = _FakeGraphForGuard([])
        s_send = _FakeGraphForReplyAll(audience)
        outcome_b, res_b = _outbox_reply_decision(
            guard_graph=g_send, send_graph=s_send,
            fake_fs=self.fake_fs, data=self.retry_data,
        )

        self.assertEqual((outcome_a, outcome_b), ("suppressed", "sent"))
        # Axis A leaked nothing because it sent nothing.
        self.assertEqual(s_supp.send_posts, [])
        # Axis B sent, but never carried operator or opted-out on the wire.
        wire_all = set()
        for payload in s_send.patched_payloads:
            for key in ("toRecipients", "ccRecipients"):
                wire_all |= {r["emailAddress"]["address"].lower() for r in payload[key]}
        self.assertTrue(wire_all)  # something did go out
        self.assertNotIn(OPERATOR_EMAIL, wire_all)
        self.assertNotIn(OPERATOR_ALIAS, wire_all)
        self.assertNotIn(OPTOUT_ALIAS, wire_all)

    # ------------------------------------------------------------------
    # Negative control: guard must NOT misfire on an unrelated conversation
    # (otherwise every reply-all would be falsely suppressed -> feature dead).
    # ------------------------------------------------------------------
    def test_guard_does_not_suppress_on_a_different_conversation(self):
        other = [{
            "id": "other-1",
            "conversationId": "SOME_OTHER_CONVERSATION",
            "subject": "RE: unrelated",
            "sentDateTime": "2026-07-04T11:00:00Z",
            "toRecipients": [{"emailAddress": {"address": BROKER}}],
        }]
        guard_graph = _FakeGraphForGuard(other)
        send_graph = _FakeGraphForReplyAll(_default_audience())

        outcome, res = _outbox_reply_decision(
            guard_graph=guard_graph,
            send_graph=send_graph,
            fake_fs=self.fake_fs,
            data=self.retry_data,
        )
        self.assertEqual(outcome, "sent",
                         "a send in an unrelated conversation must not suppress this reply")
        self.assertEqual(len(send_graph.send_posts), 1)


if __name__ == "__main__":
    unittest.main()
