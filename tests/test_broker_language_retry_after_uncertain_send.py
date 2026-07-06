"""Pressure-test: retry_after_uncertain_send.

Event class: Graph accepted a send but a *subsequent* step failed (indexing,
audit write, network timeout after createReply/createReplyAll draft send). The
message likely WENT OUT. Before any retry/requeue, the deterministic guard must
detect the already-sent copy in Sent Items so we never DOUBLE-SEND, and must
surface the uncertain-send state to the operator.

Deterministic guard under test:
  email_automation/sent_mail_guard.py
    - find_matching_sent_message_for_retry()  (the real duplicate-send guard)
    - _body_matches / _subject_matches (matching primitives)
  email_automation/dead_letter_recovery.py
    - resolve_dead_letter_item() / _requeue_verified_unsent()  (orchestration)

Boundaries faked: Graph (requests.get) returns canned Sent Items; Firestore _fs
is an in-memory fake. NO real network / sends / Firestore.

Assertions pin CORRECT behavior. Where current behavior is a SAFETY HOLE the
assertion is left RED on purpose and the bug is recorded in the summary.
"""

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import sent_mail_guard
from email_automation.sent_mail_guard import (
    find_matching_sent_message_for_retry,
    SentMailGuardLookupError,
)
from email_automation import dead_letter_recovery


SENT_AFTER = datetime(2000, 1, 1, tzinfo=timezone.utc)  # accept anything


# --------------------------------------------------------------------------- #
# Fake Graph boundary
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, value):
        self.status_code = 200
        self.headers = {}
        self._payload = {"value": value}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def make_sent_message(
    *,
    body_html,
    subject,
    to=(),
    cc=(),
    conversation_id=None,
    body_preview=None,
    msg_id="AAMk-sent-001",
):
    return {
        "id": msg_id,
        "internetMessageId": "<abc@contoso.com>",
        "conversationId": conversation_id,
        "subject": subject,
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        "bccRecipients": [],
        "sentDateTime": "2026-07-02T18:00:00Z",
        "body": {"contentType": "html", "content": body_html},
        "bodyPreview": body_preview if body_preview is not None else "",
    }


def run_guard(sent_messages, *, recipient, body, subject=None, conversation_id=None):
    """Drive the REAL guard with a faked Sent Items result set."""
    with mock.patch.object(
        sent_mail_guard.requests,
        "get",
        return_value=FakeResponse(sent_messages),
    ):
        return find_matching_sent_message_for_retry(
            {"Authorization": "Bearer x"},
            recipient=recipient,
            body=body,
            subject=subject,
            conversation_id=conversation_id,
            sent_after=SENT_AFTER,
        )


# --------------------------------------------------------------------------- #
# REAL-THREAT phrasings: the send DID happen. Guard MUST return a match
# (fire -> block the double-send). A None here = false-negative = double-send.
# --------------------------------------------------------------------------- #
class RealThreatSendHappened(unittest.TestCase):
    RECIP = "broker@acme-realty.com"

    def assert_fires(self, sent_msg, *, body, subject=None, conversation_id=None, phrasing=""):
        match = run_guard(
            [sent_msg],
            recipient=self.RECIP,
            body=body,
            subject=subject,
            conversation_id=conversation_id,
        )
        self.assertIsNotNone(
            match,
            msg=f"FALSE NEGATIVE (double-send risk) on phrasing: {phrasing!r}",
        )

    # 1. terse reply (needs identity -> supply subject)
    def test_terse(self):
        body = "Sounds good, thanks!"
        sent = make_sent_message(
            body_html=f"{body}<br><br>On Wed 7/1 broker wrote:<br>the original ask",
            subject="RE: Suite availability",
            to=[self.RECIP],
        )
        self.assert_fires(sent, body=body, subject="Suite availability", phrasing="terse reply")

    # 2. verbose long body (>800 chars)
    def test_verbose_long(self):
        body = ("Thank you for the detailed breakdown of the operating expenses. " * 20).strip()
        sent = make_sent_message(body_html=body, subject="RE: OpEx breakdown", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="OpEx breakdown", phrasing="verbose long")

    # 3. typo'd body, identical on both sides
    def test_typo(self):
        body = "Confirmign the tour for 2pm thursaday, see you thn."
        sent = make_sent_message(body_html=body, subject="RE: Tour", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="Tour", phrasing="typo'd body")

    # 4. quoted history appended to the sent copy
    def test_quoted_history(self):
        body = "Please send over the LOI draft when you have a chance."
        sent = make_sent_message(
            body_html=(
                f"<div>{body}</div><br>"
                "<div>On Mon, Jul 1, 2026, broker@acme-realty.com wrote:</div>"
                "<blockquote>What is your timeline?</blockquote>"
            ),
            subject="RE: LOI",
            to=[self.RECIP],
        )
        self.assert_fires(sent, body=body, subject="LOI", phrasing="quoted history")

    # 5. ALL CAPS
    def test_all_caps(self):
        body = "YES WE ACCEPT THE PROPOSED RATE OF $28 PSF."
        sent = make_sent_message(body_html=body, subject="RE: Rate", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="Rate", phrasing="ALL CAPS")

    # 6. signature block appended in the sent copy
    def test_signature_block(self):
        body = "Attached is the executed NDA for the Suite 400 opportunity."
        sent = make_sent_message(
            body_html=(
                f"{body}<br><br>--<br>Baylor Harrison<br>Acme Realty<br>555-123-4567"
            ),
            subject="RE: NDA",
            to=[self.RECIP],
        )
        self.assert_fires(sent, body=body, subject="NDA", phrasing="signature block")

    # 7. HTML-heavy sent copy vs plaintext stored body (entities + block tags)
    def test_html_vs_plaintext(self):
        body = "Reviewing terms & conditions for the lease at 200 Main."
        sent = make_sent_message(
            body_html="<p>Reviewing terms &amp; conditions for the lease at 200&nbsp;Main.</p>",
            subject="RE: Lease terms",
            to=[self.RECIP],
        )
        self.assert_fires(sent, body=body, subject="Lease terms", phrasing="html vs plaintext")

    # 8. leading/trailing whitespace difference
    def test_whitespace_diff(self):
        body = "   We can do the walkthrough Friday morning.  "
        sent = make_sent_message(
            body_html="We can do the walkthrough Friday morning.",
            subject="RE: Walkthrough",
            to=[self.RECIP],
        )
        self.assert_fires(sent, body=body, subject="Walkthrough", phrasing="whitespace diff")

    # 9. reply-all: recipient sits in CC, not To
    def test_reply_all_cc(self):
        body = "Looping in our finance team, please copy them going forward."
        sent = make_sent_message(
            body_html=body,
            subject="RE: Financing",
            to=["colleague@acme-realty.com"],
            cc=[self.RECIP],
        )
        self.assert_fires(sent, body=body, subject="Financing", phrasing="reply-all cc")

    # 10. multi-intent body, no subject/conv -> identity via long body
    def test_multi_intent_no_subject(self):
        body = (
            "First, yes to the site visit next week. Second, we will need parking "
            "ratios. Third, please confirm the tenant improvement allowance figure."
        )
        sent = make_sent_message(body_html=body, subject="RE: Several items", to=[self.RECIP])
        # subject omitted on the retry side -> _subject_matches skips (None)
        self.assert_fires(sent, body=body, subject=None, phrasing="multi-intent no subject")

    # 11. english Re: prefix on subject (control: guard DOES handle en)
    def test_re_prefix_english(self):
        body = "Confirmed for the 10am call tomorrow."
        sent = make_sent_message(body_html=body, subject="RE: Intro call", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="Intro call", phrasing="english Re: prefix")

    # 12. Fwd: prefix
    def test_fwd_prefix(self):
        body = "Forwarding the broker of record confirmation."
        sent = make_sent_message(body_html=body, subject="FW: BOR", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="BOR", phrasing="Fwd: prefix")

    # 13. conflicting-with-old-quote: body cites a superseded rate
    def test_conflicting_old_quote(self):
        body = "Ignore the earlier $30 PSF quote; the corrected number is $27 PSF."
        sent = make_sent_message(body_html=body, subject="RE: Corrected pricing", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="Corrected pricing", phrasing="conflicting quote")

    # 14. unicode / emoji in body
    def test_unicode_emoji(self):
        body = "Great news \U0001F389 we are moving forward with Suite 500."
        sent = make_sent_message(body_html=body, subject="RE: Suite 500", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="Suite 500", phrasing="unicode emoji")

    # 15. audit-write-failed-after-reply: exact same copy already in Sent Items
    def test_audit_write_failed_after_send(self):
        body = "Approved. Please proceed with drafting the lease documents."
        sent = make_sent_message(body_html=body, subject="RE: Lease docs", to=[self.RECIP])
        self.assert_fires(sent, body=body, subject="Lease docs", phrasing="audit write failed after send")

    # ----- REGIONAL reply prefixes: KNOWN SAFETY HOLE (left RED) ----- #
    # 16. German "AW:" reply prefix on the Sent Items copy.
    def test_regional_prefix_german_aw(self):
        body = "Wir bestaetigen den Besichtigungstermin am Freitag."
        sent = make_sent_message(
            body_html=body,
            subject="AW: Besichtigung",  # German Outlook reply prefix
            to=[self.RECIP],
        )
        # Correct behavior: the already-sent copy must still be detected.
        self.assert_fires(
            sent, body=body, subject="Besichtigung",
            phrasing="regional German AW: prefix",
        )

    # 17. Swedish "SV:" reply prefix.
    def test_regional_prefix_swedish_sv(self):
        body = "Vi bekraftar motet pa fredag."
        sent = make_sent_message(body_html=body, subject="SV: Motet", to=[self.RECIP])
        self.assert_fires(
            sent, body=body, subject="Motet",
            phrasing="regional Swedish SV: prefix",
        )

    # 18. Regional prefix even WITH a matching conversationId (strong identity)
    #     -> subject veto still throws the match away.
    def test_regional_prefix_with_conversation_id(self):
        body = "Confirmed, we will countersign today."
        sent = make_sent_message(
            body_html=body,
            subject="AW: Countersignature",
            to=[self.RECIP],
            conversation_id="CONV-777",
        )
        self.assert_fires(
            sent, body=body, subject="Countersignature",
            conversation_id="CONV-777",
            phrasing="regional prefix with matching conversationId",
        )


# --------------------------------------------------------------------------- #
# NEAR-MISS controls: guard MUST NOT fire (return None). Firing = false positive
# blocking a legitimately-new / never-sent broker email.
# --------------------------------------------------------------------------- #
class NearMissNoMatch(unittest.TestCase):
    RECIP = "broker@acme-realty.com"

    # NM1: "Graph failed before any send attempt." Sent Items holds only
    # unrelated traffic -> nothing to reconcile -> None -> requeue is safe.
    def test_failed_before_send_no_copy(self):
        other = make_sent_message(
            body_html="Totally unrelated message about a different deal.",
            subject="RE: Other deal",
            to=["someone-else@example.com"],
        )
        match = run_guard(
            [other],
            recipient=self.RECIP,
            body="Confirming the tour for 2pm Thursday.",
            subject="Tour",
        )
        self.assertIsNone(match, "FALSE POSITIVE: matched despite no send having occurred")

    # NM2: "Retry item has different body from prior attempt."
    def test_different_body(self):
        sent = make_sent_message(
            body_html="We regret we must pass on this opportunity at this time.",
            subject="RE: Suite 400",
            to=[self.RECIP],
        )
        match = run_guard(
            [sent],
            recipient=self.RECIP,
            body="We are excited to move forward and accept your terms.",
            subject="Suite 400",
        )
        self.assertIsNone(match, "FALSE POSITIVE: matched a genuinely different body")

    # NM3: "Retry item has different recipient from prior attempt."
    def test_different_recipient(self):
        body = "Confirming the tour for 2pm Thursday."
        sent = make_sent_message(body_html=body, subject="RE: Tour", to=["other@elsewhere.com"])
        match = run_guard([sent], recipient=self.RECIP, body=body, subject="Tour")
        self.assertIsNone(match, "FALSE POSITIVE: matched despite different recipient")

    # NM4: different conversation entirely
    def test_different_conversation(self):
        body = "Confirming the tour for 2pm Thursday, see you then."
        sent = make_sent_message(
            body_html=body, subject="RE: Tour", to=[self.RECIP], conversation_id="CONV-A"
        )
        match = run_guard(
            [sent], recipient=self.RECIP, body=body, subject="Tour", conversation_id="CONV-B"
        )
        self.assertIsNone(match, "FALSE POSITIVE: matched across a different conversationId")


# --------------------------------------------------------------------------- #
# Identity-safety control: a terse retry with NO subject/conv and a short body
# must NOT be silently matched on thin evidence -> guard RAISES so the caller
# fails safe (block + operator review), never a blind match.
# --------------------------------------------------------------------------- #
class IdentitySafety(unittest.TestCase):
    def test_thin_identity_raises(self):
        with self.assertRaises(SentMailGuardLookupError):
            run_guard(
                [make_sent_message(body_html="ok", subject="", to=["b@x.com"])],
                recipient="b@x.com",
                body="ok",  # <80 chars, no subject, no conv
            )


# --------------------------------------------------------------------------- #
# Fake Firestore + end-to-end orchestration (shows the double-send actually
# reaches requeue when the guard misses).
# --------------------------------------------------------------------------- #
class FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class FakeDocRef:
    def __init__(self, store, key):
        self._store = store
        self._key = key
        self.id = key[-1] if key else "doc"

    def get(self):
        return FakeSnapshot(self._store.get(self._key))

    def collection(self, name):
        return FakeCollection(self._store, self._key + (name,))

    def update(self, payload):
        self._store.setdefault(self._key, {}).update(payload)

    def set(self, payload, merge=False):
        if merge:
            self._store.setdefault(self._key, {}).update(payload)
        else:
            self._store[self._key] = dict(payload)


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
        key = self._prefix + (f"auto-{self._auto}",)
        self._store[key] = dict(payload)
        return (None, FakeDocRef(self._store, key))


class FakeFirestore:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCollection(self.store, (name,))


class EndToEndDoubleSend(unittest.TestCase):
    RECIP = "broker@acme-realty.com"

    def _resolve(self, dead_letter_data, sent_messages):
        fake_fs = FakeFirestore()
        # seed the dead-letter doc
        fake_fs.store[("users", "user-1", "deadLetterQueue", "dl-1")] = dead_letter_data
        with mock.patch("email_automation.clients._fs", fake_fs, create=True), \
             mock.patch.object(
                 sent_mail_guard.requests, "get",
                 return_value=FakeResponse(sent_messages),
             ):
            result = dead_letter_recovery.resolve_dead_letter_item(
                "user-1", "dl-1",
                action="requeue_verified_unsent",
                headers={"Authorization": "Bearer x"},
                operator_id="op-1",
            )
        return result, fake_fs

    def test_uncertain_send_english_is_caught(self):
        """Control: English Re: prefix -> guard catches -> already_sent, no requeue."""
        body = "Approved, please proceed with the lease documents now."
        dl = {
            "source": "outbox",
            "sentRecipients": [self.RECIP],
            "script": body,
            "subject": "Lease docs",
            "failureReason": "Audit write failed after reply send",
        }
        sent = [make_sent_message(body_html=body, subject="RE: Lease docs", to=[self.RECIP])]
        result, _ = self._resolve(dl, sent)
        self.assertEqual(
            result.get("code"), "already_sent",
            msg="English-prefix uncertain send should be caught as already_sent",
        )

    def test_uncertain_send_regional_double_sends(self):
        """SAFETY HOLE: regional reply prefix + no stored conversationId (the
        classic uncertain-send: we never captured the Graph response) -> guard
        misses -> item is REQUEUED -> the broker gets the SAME email twice."""
        body = "Wir bestaetigen den Besichtigungstermin am Freitag um 10 Uhr."
        dl = {
            "source": "outbox",
            "sentRecipients": [self.RECIP],
            "script": body,
            "subject": "Besichtigung",     # stored subject, no regional prefix
            # NOTE: no conversationId (uncertain send -> response never read)
            "failureReason": "Network timeout after createReplyAll draft send",
        }
        sent = [make_sent_message(body_html=body, subject="AW: Besichtigung", to=[self.RECIP])]
        result, _ = self._resolve(dl, sent)
        # CORRECT behavior: the already-sent copy is detected -> already_sent.
        self.assertEqual(
            result.get("code"), "already_sent",
            msg=(
                "DOUBLE-SEND: regional reply-prefix (AW:) uncertain send was "
                f"requeued instead of reconciled. result={result}"
            ),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
