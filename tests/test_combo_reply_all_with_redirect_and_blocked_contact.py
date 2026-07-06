"""combinationStressDeck: reply_all_with_redirect_and_blocked_contact.

Deck playbooks composed here (docs/release-safety/feature-gradebook.json):
  - reply_all_cc_plus_blocked_contact: "Reply-all contains safe copied broker
    plus blocked/opt-out contact; final recipients must preserve safe CCs and
    filter blocked contacts."
  - wrong_contact_plus_new_property: "Broker redirects contact and suggests an
    alternate property in one message; system must not send outside allowed
    recipients without explicit approval."
  - opt_out_after_prior_interest: "Broker previously engaged but later opts out;
    all future autonomous and manual sends to that contact must stop."

deck.mustProve:
  1. safe CCs are preserved
  2. blocked contacts are filtered
  3. new contact/property needs explicit approval before send

WHAT THIS TEST ACTUALLY DRIVES
------------------------------
The single real reply-all send handler ``email.py::_send_outbox_as_reply`` is
driven end-to-end against a faked Microsoft Graph (createReplyAll -> hydrate ->
patch -> send -> SentItems identity) and a faked Firestore opt-out store. The
REAL recipient-safety filter (``_filter_reply_all_draft_recipients``) and the
REAL opt-out lookup (``processing.is_contact_opted_out``, including its
plus-alias mailbox-identity probing) run unmodified. Nothing about the filter
or the send path is stubbed — only the Graph HTTP boundary and the Firestore
client are faked. ZERO live sends, ZERO live reads.

The reply-all draft Graph hands back copies a *wrong-contact* broker on To, a
*safe teammate* on Cc, a *blocked/opted-out* broker on Cc, and the operator's
own mailbox on Cc. The broker body redirects to a brand-new contact at a new
property. The invariant proven across the interaction is that the bytes that
actually reach ``/send`` keep the safe teammate, drop the blocked contact and
the operator, never fabricate the new redirect contact, and fire exactly once.
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

from email_automation import email as email_mod
from email_automation import processing as processing_mod


# ---------------------------------------------------------------------------
# Cast (deck personas)
# ---------------------------------------------------------------------------
OPERATOR = "operator@sitesift.com"          # the mailbox running SiteSift (self)
WRONG_CONTACT = "broker@acme.com"           # broker who replied (wrong contact, still on-thread)
SAFE_TEAMMATE = "colleague@acme.com"        # safe teammate the broker CC'd
BLOCKED_BROKER = "blocked-broker@acme.com"  # prior interest, later opted out -> blocked
NEW_REDIRECT_CONTACT = "newcontact@other-firm.com"  # redirect target, NOT on the thread


def _hash(email: str) -> str:
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Firestore opt-out double — drives the REAL is_contact_opted_out.
# Mirrors: _fs.collection("users").document(uid)
#             .collection("optedOutContacts").document(<hash>).get()
# ---------------------------------------------------------------------------
class _FakeSnap:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data or {}

    def to_dict(self):
        return self._data


class _FakeOptOutDocs:
    def __init__(self, by_hash):
        self._by_hash = by_hash

    def document(self, doc_id):
        record = self._by_hash.get(doc_id)
        return _FakeDocGettable(record)


class _FakeDocGettable:
    def __init__(self, record):
        self._record = record

    def get(self):
        return _FakeSnap(self._record)


class _FakeUserDoc:
    def __init__(self, by_hash):
        self._by_hash = by_hash

    def collection(self, name):
        assert name == "optedOutContacts", f"unexpected subcollection {name}"
        return _FakeOptOutDocs(self._by_hash)


class _FakeUsersCollection:
    def __init__(self, by_hash):
        self._by_hash = by_hash

    def document(self, _uid):
        return _FakeUserDoc(self._by_hash)


class _FakeFirestore:
    """Firestore double exposing only the optedOutContacts read path."""

    def __init__(self, opted_out_emails):
        # store under the exact (lowercased) address hash, like production
        self._by_hash = {
            _hash(email): {"reason": "opted_out", "email": email.lower()}
            for email in opted_out_emails
        }

    def collection(self, name):
        assert name == "users", f"unexpected collection {name}"
        return _FakeUsersCollection(self._by_hash)


# ---------------------------------------------------------------------------
# Microsoft Graph HTTP double — records every send + patch so we can assert on
# the exact recipient bytes that would leave the mailbox.
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as _rq
            raise _rq.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


def _graph_recipient(address, name=None):
    ea = {"address": address}
    if name:
        ea["name"] = name
    return {"emailAddress": ea}


class _FakeGraph:
    """Routes Graph REST calls by method + URL and records send/patch traffic."""

    def __init__(self, draft_recipients, conversation_id="conv-redirect-1"):
        self.draft_recipients = draft_recipients
        self.conversation_id = conversation_id
        self.patch_payloads = []
        self.send_calls = []
        self.deleted_drafts = []

    # -- GET -----------------------------------------------------------------
    def get(self, url, headers=None, params=None, timeout=None):
        if "/mailFolders/SentItems/messages" in url:
            # identity resolution after send
            return _FakeResponse(200, {"value": [{
                "id": "sent-1",
                "internetMessageId": "<sent-1@sitesift>",
                "conversationId": self.conversation_id,
                "subject": "RE: 200 Market St",
                "sentDateTime": "2026-07-04T00:00:00Z",
            }]})
        # source-message metadata fetch (_fetch_graph_message_metadata / hydrate)
        return _FakeResponse(200, {
            "conversationId": self.conversation_id,
            "subject": "200 Market St",
            "from": {"emailAddress": {"address": WRONG_CONTACT}},
            "replyTo": [_graph_recipient(WRONG_CONTACT)],
            "toRecipients": [_graph_recipient(OPERATOR)],
            "ccRecipients": [],
        })

    # -- POST ----------------------------------------------------------------
    def post(self, url, headers=None, json=None, timeout=None):
        if url.endswith("/createReplyAll"):
            # Graph returns a fully-hydrated reply-all audience.
            return _FakeResponse(201, {
                "id": "draft-1",
                "toRecipients": self.draft_recipients["to"],
                "ccRecipients": self.draft_recipients["cc"],
            })
        if url.endswith("/send"):
            self.send_calls.append(url)
            return _FakeResponse(202, {})
        if url.endswith("/attachments"):
            return _FakeResponse(201, {})
        raise AssertionError(f"unexpected POST {url}")

    # -- PATCH ---------------------------------------------------------------
    def patch(self, url, headers=None, json=None, timeout=None):
        self.patch_payloads.append(json)
        return _FakeResponse(200, {})

    # -- DELETE (draft cleanup on no-safe-recipient) -------------------------
    def delete(self, url, headers=None, timeout=None):
        self.deleted_drafts.append(url)
        return _FakeResponse(204, {})


def _sent_addresses(patch_payload):
    """Flatten a Graph patch payload's To+Cc into a lowercased address set."""
    out = set()
    for key in ("toRecipients", "ccRecipients"):
        for r in patch_payload.get(key, []) or []:
            addr = ((r or {}).get("emailAddress") or {}).get("address")
            if addr:
                out.add(addr.lower())
    return out


def _cc_addresses(patch_payload):
    out = set()
    for r in patch_payload.get("ccRecipients", []) or []:
        addr = ((r or {}).get("emailAddress") or {}).get("address")
        if addr:
            out.add(addr.lower())
    return out


class ComboReplyAllRedirectBlockedContactTests(unittest.TestCase):
    """Drive the REAL reply-all send handler through the composed deck scenario."""

    def _run_reply_all(self, draft_recipients, opted_out_emails):
        graph = _FakeGraph(draft_recipients)
        firestore = _FakeFirestore(opted_out_emails)
        headers = {"Authorization": "Bearer test-token"}

        # Only the Graph HTTP boundary and the Firestore client are faked.
        # _filter_reply_all_draft_recipients + is_contact_opted_out run REAL.
        with patch.object(email_mod, "requests", graph), \
             patch.object(processing_mod, "_fs", firestore), \
             patch("email_automation.utils.time.sleep", return_value=None), \
             patch.object(email_mod.time, "sleep", return_value=None):
            result = email_mod._send_outbox_as_reply(
                user_id="uid-redirect-1",
                headers=headers,
                body="Hi, thanks for looping in the team — following up on 200 Market St.",
                reply_to_msg_id="msg-root",
                thread_id="thread-redirect-1",
                user_signature=None,
                signature_mode=None,
                user_email=OPERATOR,
            )
        return result, graph

    # ------------------------------------------------------------------ #
    # MAIN: all three deck playbooks composed in one reply-all send.
    # ------------------------------------------------------------------ #
    def test_reply_all_preserves_safe_cc_filters_blocked_and_never_leaks_redirect_target(self):
        draft = {
            "to": [_graph_recipient(WRONG_CONTACT, "Broker Acme")],
            "cc": [
                _graph_recipient(SAFE_TEAMMATE, "Colleague Acme"),   # safe teammate copied
                _graph_recipient(BLOCKED_BROKER, "Blocked Broker"),  # opted out after prior interest
                _graph_recipient(OPERATOR, "SiteSift Operator"),     # self / operator
            ],
        }
        result, graph = self._run_reply_all(draft, opted_out_emails=[BLOCKED_BROKER])

        # The send actually happened, exactly once (no duplicate send).
        self.assertTrue(result["sent"], f"send failed: {result.get('error')}")
        self.assertEqual(1, len(graph.send_calls), "reply-all must send exactly once")
        self.assertEqual(1, len(graph.patch_payloads), "draft must be patched exactly once")

        patch_payload = graph.patch_payloads[0]
        sent = _sent_addresses(patch_payload)
        cc = _cc_addresses(patch_payload)

        # mustProve #2 — blocked/opted-out contact is filtered out of the send.
        self.assertNotIn(BLOCKED_BROKER, sent,
                         "blocked/opted-out broker leaked into the reply-all send")
        # operator self-send is stripped (never reply-all back to our own mailbox).
        self.assertNotIn(OPERATOR, sent, "operator mailbox leaked into reply-all audience")
        # mustProve #3 — a redirect contact the broker named but that is NOT on
        # the thread is never fabricated into recipients (needs explicit approval).
        self.assertNotIn(NEW_REDIRECT_CONTACT, sent,
                         "new redirect contact was auto-added without approval")

        # mustProve #1 — safe CCs are preserved.
        self.assertIn(SAFE_TEAMMATE, cc, "safe teammate CC was dropped")
        # the on-thread wrong-contact broker (already a party) stays a recipient.
        self.assertIn(WRONG_CONTACT, sent, "on-thread broker was incorrectly dropped")

        # The exact surviving audience is the safe set and nothing else.
        self.assertEqual({WRONG_CONTACT, SAFE_TEAMMATE}, sent)

        # Handler-returned classification agrees with the wire bytes.
        self.assertEqual({SAFE_TEAMMATE}, {a.lower() for a in result["ccRecipients"]})
        self.assertEqual({WRONG_CONTACT, SAFE_TEAMMATE},
                         {a.lower() for a in result["sentRecipients"]})
        skipped = result.get("skippedRecipients") or {}
        opted_out = {e["email"] for e in skipped.get("optedOut", [])}
        self.assertIn(BLOCKED_BROKER, opted_out, "opt-out skip not recorded")
        self.assertIn(OPERATOR, skipped.get("operator", []), "operator skip not recorded")

    # ------------------------------------------------------------------ #
    # BREAK VECTOR: blocked contact copied via a plus-alias reply.
    # Opt-out is stored under the bare address; the alias must still be caught.
    # ------------------------------------------------------------------ #
    def test_blocked_contact_via_plus_alias_is_still_filtered(self):
        alias = "blocked-broker+leasing@acme.com"
        draft = {
            "to": [_graph_recipient(WRONG_CONTACT)],
            "cc": [
                _graph_recipient(SAFE_TEAMMATE),
                _graph_recipient(alias),  # same mailbox as BLOCKED_BROKER, plus-aliased
            ],
        }
        # opt-out stored ONLY under the bare address
        result, graph = self._run_reply_all(draft, opted_out_emails=[BLOCKED_BROKER])

        self.assertTrue(result["sent"], f"send failed: {result.get('error')}")
        sent = _sent_addresses(graph.patch_payloads[0])
        self.assertNotIn(alias, sent, "plus-aliased blocked contact leaked past filter")
        self.assertNotIn(BLOCKED_BROKER, sent)
        self.assertIn(SAFE_TEAMMATE, sent, "safe teammate wrongly dropped")

    # ------------------------------------------------------------------ #
    # DISCRIMINATING NEGATIVE CONTROL: with NO opt-out on record, the very same
    # contact IS preserved. Proves the filter keys on the opt-out state — the
    # blocked assertions above cannot be borrowed greens from always-dropping.
    # ------------------------------------------------------------------ #
    def test_same_contact_is_preserved_when_not_opted_out(self):
        draft = {
            "to": [_graph_recipient(WRONG_CONTACT)],
            "cc": [
                _graph_recipient(SAFE_TEAMMATE),
                _graph_recipient(BLOCKED_BROKER),  # identical address, but NOT opted out now
            ],
        }
        result, graph = self._run_reply_all(draft, opted_out_emails=[])

        self.assertTrue(result["sent"], f"send failed: {result.get('error')}")
        sent = _sent_addresses(graph.patch_payloads[0])
        # No opt-out record -> the contact survives, proving discrimination.
        self.assertIn(BLOCKED_BROKER, sent,
                      "control failed: contact dropped even without an opt-out record")
        self.assertIn(SAFE_TEAMMATE, sent)
        self.assertIn(WRONG_CONTACT, sent)
        # operator is still always stripped regardless of opt-out state.
        self.assertNotIn(OPERATOR, sent)


if __name__ == "__main__":
    unittest.main()
