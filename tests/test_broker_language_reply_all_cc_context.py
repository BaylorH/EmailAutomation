"""
Pressure test for the reply_all_cc_context broker-event class.

DETERMINISTIC GUARD UNDER TEST:
    email_automation.email._filter_reply_all_draft_recipients

This is the real function that decides who survives on a reply-all draft before
send. It preserves Microsoft Graph's reply-all audience but strips unsafe
recipients: the operator's own address, duplicates, malformed addresses, and
opted-out / blocked contacts (via the REAL email_automation.processing
.is_contact_opted_out, itself driven by a fake Firestore boundary here).

Safety contract for this event (from the seeds / stopIf):
    * A SAFE CC (leasing teammate, assistant, original broker) must NOT be dropped.
    * A QUOTED recipient (only present in old body history) must NOT be added.
    * A BLOCKED / opted-out recipient must NOT survive.

We drive the REAL function on 18+ messy reply-all shapes and assert the guard
fires on real threats (removes unsafe) and stays quiet on near-misses (keeps
safe). External boundaries faked: Firestore _fs only. No Graph / Sheets / sends.
"""

import hashlib
import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.email import _filter_reply_all_draft_recipients
from email_automation import processing


USER_ID = "user-under-test"
OPERATOR = "agent@sitesift.com"


def _hash_email(email: str) -> str:
    email_lower = email.lower().strip()
    return hashlib.sha256(email_lower.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Fake Firestore boundary — drives the REAL is_contact_opted_out.
# --------------------------------------------------------------------------- #
class _FakeDoc:
    def __init__(self, exists, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return self._data


class _FakeDocRef:
    def __init__(self, store, doc_id, collection_name):
        self._store = store
        self._doc_id = doc_id
        self._collection_name = collection_name

    def collection(self, name):
        return _FakeCollection(self._store, name)

    def get(self):
        if self._collection_name == "optedOutContacts":
            if self._doc_id in self._store["optedOutHashes"]:
                return _FakeDoc(True, {"reason": "unsubscribed"})
            return _FakeDoc(False)
        return _FakeDoc(False)


class _FakeCollection:
    def __init__(self, store, name):
        self._store = store
        self._name = name

    def document(self, doc_id):
        return _FakeDocRef(self._store, doc_id, self._name)


class _FakeFS:
    def __init__(self, opted_out_emails):
        self._store = {
            "optedOutHashes": {_hash_email(e) for e in opted_out_emails},
        }

    def collection(self, name):
        return _FakeCollection(self._store, name)


# --------------------------------------------------------------------------- #
# Draft-building helpers (Microsoft Graph recipient shape).
# --------------------------------------------------------------------------- #
def _rcpt(address, name=None):
    ea = {"address": address}
    if name:
        ea["name"] = name
    return {"emailAddress": ea}


def _draft(to=None, cc=None, body=None):
    d = {"toRecipients": list(to or []), "ccRecipients": list(cc or [])}
    if body is not None:
        d["body"] = {"contentType": "text", "content": body}
    return d


def _addrs(recipients):
    out = []
    for r in recipients:
        a = ((r.get("emailAddress") or {}).get("address") or "").strip().lower()
        if a:
            out.append(a)
    return out


class ReplyAllCcContextTest(unittest.TestCase):
    def _run(self, draft, opted_out=(), user_email=OPERATOR):
        """Drive the REAL guard with a fake Firestore behind the REAL opt-out check."""
        with mock.patch.object(processing, "_fs", _FakeFS(opted_out)):
            return _filter_reply_all_draft_recipients(
                USER_ID, draft, user_email=user_email
            )

    def _to(self, result):
        return _addrs(result["payload"]["toRecipients"])

    def _cc(self, result):
        return _addrs(result["payload"]["ccRecipients"])

    def _all(self, result):
        return self._to(result) + self._cc(result)

    # ===================================================================== #
    # SAFE-PRESERVE phrasings — dropping any of these violates
    # stopIf["safe CC is dropped"].
    # ===================================================================== #
    def test_p01_two_leasing_teammates_copied(self):
        # "Broker replies with two leasing teammates copied."
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("teammate1@realty.com"), _rcpt("teammate2@realty.com")],
        )
        r = self._run(d)
        self.assertIn("broker@realty.com", self._to(r))
        self.assertIn("teammate1@realty.com", self._cc(r))
        self.assertIn("teammate2@realty.com", self._cc(r))

    def test_p02_assistant_from_copied_addr_original_broker_on_thread(self):
        # "Assistant replies from copied address while original broker remains."
        d = _draft(
            to=[_rcpt("assistant@realty.com")],
            cc=[_rcpt("broker@realty.com")],
        )
        r = self._run(d)
        self.assertIn("assistant@realty.com", self._all(r))
        self.assertIn("broker@realty.com", self._all(r))

    def test_p03_safe_cc_with_subdomain(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("leasing@na.cbre.com")],
        )
        r = self._run(d)
        self.assertIn("leasing@na.cbre.com", self._cc(r))

    def test_p04_safe_cc_with_display_name_preserved(self):
        d = _draft(
            to=[_rcpt("broker@realty.com", "Pat Broker")],
            cc=[_rcpt("teammate@realty.com", "Sam Teammate")],
        )
        r = self._run(d)
        names = [
            ((x.get("emailAddress") or {}).get("name") or "")
            for x in r["payload"]["ccRecipients"]
        ]
        self.assertIn("teammate@realty.com", self._cc(r))
        self.assertIn("Sam Teammate", names)

    def test_p05_forwarded_chain_safe_ccs_preserved(self):
        # "Forwarded chain includes safe CCs and unrelated quoted recipients."
        # (Structured recipients are all live/safe; quoted ones live only in body.)
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("cobroker@partners.com"), _rcpt("analyst@realty.com")],
            body="On Mon, someone wrote:\n> Cc: stale-old-guy@ancient.com",
        )
        r = self._run(d)
        self.assertIn("cobroker@partners.com", self._cc(r))
        self.assertIn("analyst@realty.com", self._cc(r))

    def test_p06_lowercase_and_mixed_case_safe_addr_kept_once(self):
        d = _draft(
            to=[_rcpt("Broker@Realty.com")],
            cc=[_rcpt("Teammate@Realty.com")],
        )
        r = self._run(d)
        self.assertIn("broker@realty.com", self._to(r))
        self.assertIn("teammate@realty.com", self._cc(r))

    # ===================================================================== #
    # NEAR-MISS controls — these must NOT change the live audience.
    # ===================================================================== #
    def test_nm01_quoted_cc_lines_in_body_do_not_become_recipients(self):
        # "Quoted CC lines in old history should not become live recipients."
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("teammate@realty.com")],
            body=(
                "Thanks!\n\n"
                "-------- Original Message --------\n"
                "From: oldcontact@ghost.com\n"
                "To: broker@realty.com\n"
                "Cc: quoted-stale@ghost.com, another-ghost@ghost.com\n"
            ),
        )
        r = self._run(d)
        allrec = self._all(r)
        self.assertNotIn("quoted-stale@ghost.com", allrec)
        self.assertNotIn("another-ghost@ghost.com", allrec)
        self.assertNotIn("oldcontact@ghost.com", allrec)
        # ...and the live audience is intact.
        self.assertIn("broker@realty.com", allrec)
        self.assertIn("teammate@realty.com", allrec)

    def test_nm02_blocked_contact_in_cc_is_removed(self):
        # "Blocked/opt-out contact appears in CC."  -> must not survive.
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("teammate@realty.com"), _rcpt("blocked@optout.com")],
        )
        r = self._run(d, opted_out=["blocked@optout.com"])
        self.assertNotIn("blocked@optout.com", self._all(r))
        self.assertIn("teammate@realty.com", self._cc(r))
        self.assertIn("broker@realty.com", self._to(r))

    # ===================================================================== #
    # REAL-THREAT phrasings — guard must FIRE (strip the unsafe recipient).
    # ===================================================================== #
    def test_t01_opted_out_in_cc_terse(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("nope@optout.com")],
        )
        r = self._run(d, opted_out=["nope@optout.com"])
        self.assertNotIn("nope@optout.com", self._all(r))

    def test_t02_opted_out_in_to_position(self):
        d = _draft(
            to=[_rcpt("blocked@optout.com")],
            cc=[_rcpt("teammate@realty.com")],
        )
        r = self._run(d, opted_out=["blocked@optout.com"])
        self.assertNotIn("blocked@optout.com", self._all(r))
        self.assertIn("teammate@realty.com", self._cc(r))

    def test_t03_operator_own_address_copied_on_thread_removed(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt(OPERATOR)],
        )
        r = self._run(d)
        self.assertNotIn(OPERATOR.lower(), self._all(r))
        self.assertIn("broker@realty.com", self._to(r))

    def test_t04_duplicate_broker_in_to_and_cc_deduped(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("broker@realty.com"), _rcpt("teammate@realty.com")],
        )
        r = self._run(d)
        self.assertEqual(self._all(r).count("broker@realty.com"), 1)
        self.assertIn("teammate@realty.com", self._cc(r))

    def test_t05_case_variant_duplicate_deduped(self):
        d = _draft(
            to=[_rcpt("Broker@Realty.com")],
            cc=[_rcpt("broker@realty.com")],
        )
        r = self._run(d)
        self.assertEqual(self._all(r).count("broker@realty.com"), 1)

    def test_t06_garbage_address_removed(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("notanemail"), _rcpt("teammate@realty.com")],
        )
        r = self._run(d)
        self.assertNotIn("notanemail", self._all(r))
        self.assertIn("teammate@realty.com", self._cc(r))

    def test_t07_opted_out_with_whitespace_and_caps_removed(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("  BLOCKED@Optout.com  ")],
        )
        r = self._run(d, opted_out=["blocked@optout.com"])
        self.assertNotIn("blocked@optout.com", self._all(r))

    def test_t08_multiple_opted_out_mixed_with_safe(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[
                _rcpt("out1@optout.com"),
                _rcpt("teammate@realty.com"),
                _rcpt("out2@optout.com"),
            ],
        )
        r = self._run(d, opted_out=["out1@optout.com", "out2@optout.com"])
        allrec = self._all(r)
        self.assertNotIn("out1@optout.com", allrec)
        self.assertNotIn("out2@optout.com", allrec)
        self.assertIn("teammate@realty.com", allrec)

    def test_t09_reserved_tld_address_removed(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("someone@internal.test")],
        )
        r = self._run(d)
        self.assertNotIn("someone@internal.test", self._all(r))

    def test_t10_empty_recipient_dict_dropped_without_crash(self):
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[{"emailAddress": {}}, {}],
        )
        r = self._run(d)
        self.assertIn("broker@realty.com", self._to(r))
        self.assertEqual(self._cc(r), [])

    def test_t11_all_caps_verbose_thread_safe_ones_kept(self):
        d = _draft(
            to=[_rcpt("BROKER@REALTY.COM")],
            cc=[_rcpt("LEASING.TEAM@REALTY.COM"), _rcpt("BLOCKED@OPTOUT.COM")],
        )
        r = self._run(d, opted_out=["blocked@optout.com"])
        allrec = self._all(r)
        self.assertIn("broker@realty.com", allrec)
        self.assertIn("leasing.team@realty.com", allrec)
        self.assertNotIn("blocked@optout.com", allrec)

    # ===================================================================== #
    # BUG PROBES — assertions written to the CORRECT (safe) behavior.
    # If they fail RED, the guard has a real safety hole.
    # ===================================================================== #
    def test_bug01_optout_plus_alias_bypass(self):
        """
        A contact opts out as broker@x.com. The reply-all thread copies the
        SAME mailbox under a plus alias broker+leasing@x.com. Same person, same
        inbox -> must still be treated as opted out and stripped.

        The opt-out lookup hashes the exact address, so the plus alias slips
        through even though the codebase already has _mailbox_identity_without_plus.
        This is an opt-out / blocked-recipient bypass ("blocked recipient is added").
        """
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("broker+leasing@x.com")],
        )
        r = self._run(d, opted_out=["broker@x.com"])
        self.assertNotIn(
            "broker+leasing@x.com",
            self._all(r),
            "opted-out mailbox reached via plus alias — opt-out bypass",
        )

    def test_bug02_operator_plus_alias_self_send(self):
        """
        The automation's own mailbox appears on the thread under its plus alias
        (agent+campaign1@sitesift.com). Replying to it delivers back to the
        operator's own inbox (self-send / potential auto-processing loop). The
        operator check compares the exact normalized address only, so the alias
        survives.
        """
        d = _draft(
            to=[_rcpt("broker@realty.com")],
            cc=[_rcpt("agent+campaign1@sitesift.com")],
        )
        r = self._run(d)
        self.assertNotIn(
            "agent+campaign1@sitesift.com",
            self._all(r),
            "operator's own mailbox (plus alias) kept — self-send",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
