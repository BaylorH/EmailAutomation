"""
Pressure test: broker_opt_out (contact_optout) SAFETY guard.

EVENT CLASS: broker_opt_out
  Real phrasings: "Please remove me from your list.", "Do not contact me
  about this again.", "Unsubscribe."
  stopIf: follow-up sends after opt-out; blocked address reappears through CC.

WHERE THE DECISION LIVES
------------------------
  * DETECTION (broker language -> contact_optout event) is LLM-ONLY. The
    prompt in email_automation/ai_processing.py (lines ~997-1005) instructs
    the model to emit "contact_optout" for opt-out language. The deterministic
    augmentation `_augment_events_with_deterministic_signals` has NO branch for
    contact_optout (it only backstops property_unavailable / tour_requested).
    So there is no deterministic detector to drive broker *language* against.

  * SUPPRESSION (the safety behavior that stopIf actually targets) IS
    deterministic and testable:
        - email_automation.processing.is_contact_opted_out(user_id, email)
          -> the guard every send path consults.
        - email_automation.processing._store_contact_optout(...)
          -> records the opt-out (keyed by sha256 of email.lower().strip()).
        - email_automation.email._filter_reply_all_draft_recipients(...)
          -> drops opted-out To/CC recipients before a reply-all send. This is
          the exact guard for stopIf "blocked address reappears through CC".
        - followup.py / send_and_index / multi-property senders all gate on
          is_contact_opted_out (stopIf "follow-up sends after opt-out").

WHAT THIS FILE DRIVES
---------------------
  The REAL suppression guard, on the messy real-world ways an opted-out
  broker's ADDRESS reappears at send time (case, whitespace, display-name
  wrapping, To vs CC, reappearance in both). Those address forms are the
  "phrasings" the deterministic guard actually sees.

  Real-threats  -> the guard MUST suppress (no false negative == no send to an
                   opted-out broker).
  Near-misses   -> a *different* address (e.g. a wrong_contact redirect target)
                   MUST NOT be suppressed (no false positive == legit broker
                   email still goes out).

Only external boundaries are faked (Firestore _fs). No Graph / Sheets / real
sends occur. Assertions that pin CURRENT WRONG behavior are written to the
CORRECT expectation so they fail RED and flag the bug.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")

import email_automation.processing as P
import email_automation.email as E
import email_automation.ai_processing as A


# --------------------------------------------------------------------------
# Fake Firestore (only the chain these guards touch)
#   _fs.collection(...).document(...).collection(...).document(hash)
#        .set(data) / .get() -> snapshot(.exists, .to_dict()) / .delete()
# --------------------------------------------------------------------------
class _Snap:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _DocRef:
    def __init__(self, fs, path):
        self.fs = fs
        self.path = path
        self.reference = self

    def collection(self, name):
        return _Node(self.fs, self.path + (name,))

    def set(self, data, merge=False):
        if merge and self.path in self.fs.docs:
            self.fs.docs[self.path].update(data)
        else:
            self.fs.docs[self.path] = dict(data)

    def get(self):
        if self.fs.raise_all_get:
            raise RuntimeError("Firestore transient error (simulated)")
        return _Snap(self.fs.docs.get(self.path))

    def delete(self):
        self.fs.docs.pop(self.path, None)


class _Node:
    def __init__(self, fs, path):
        self.fs = fs
        self.path = path

    def document(self, doc_id):
        return _DocRef(self.fs, self.path + (doc_id,))

    def collection(self, name):
        return _Node(self.fs, self.path + (name,))


class FakeFirestore:
    def __init__(self):
        self.docs = {}
        self.raise_all_get = False

    def collection(self, name):
        return _Node(self, (name,))


USER = "user-1"
THREAD = "thread-1"
OPTED_OUT = "dana.broker@acme-realty.com"   # the broker who said "unsubscribe"

# 15 messy real-world forms of the SAME opted-out mailbox. Each must be suppressed.
REAL_THREAT_FORMS = [
    ("exact_lowercase",        OPTED_OUT),
    ("all_uppercase",          "DANA.BROKER@ACME-REALTY.COM"),
    ("mixed_case",             "Dana.Broker@Acme-Realty.Com"),
    ("leading_trailing_space", "   dana.broker@acme-realty.com   "),
    ("tab_newline_padding",    "\tdana.broker@acme-realty.com\n"),
    ("domain_upper_only",      "dana.broker@ACME-REALTY.COM"),
    ("local_upper_only",       "DANA.BROKER@acme-realty.com"),
    ("nbsp_padding",           " dana.broker@acme-realty.com "),
]

# Near-misses: genuinely DIFFERENT mailboxes that must NOT be suppressed.
NEAR_MISS_ADDRESSES = [
    ("wrong_contact_redirect_target", "other.broker@acme-realty.com"),
    ("typo_domain_diff_mailbox",      "dana.broker@acme-realtyy.com"),
    ("numbered_variant_diff_mailbox", "dana.broker2@acme-realty.com"),
]

# Opt-out broker LANGUAGE phrasings (15+). Detection is LLM-only; used only to
# characterize the deterministic augmentation (it must not silently reclassify
# opt-out language into a terminal property_unavailable / tour signal).
OPT_OUT_PHRASINGS = [
    "Please remove me from your list.",
    "Do not contact me about this again.",
    "Unsubscribe.",
    "please REMOVE me from your mailing list, thanks",
    "stop emailing me",
    "Take me off your list.",
    "no longer interested, please stop",
    "We don't work with tenant reps.",
    "pls unsub",  # terse / typo
    "Kindly cease all further communication regarding this and any other matter.",
    "DO NOT EMAIL ME AGAIN",  # all caps
    "not interested\n\nDana Broker\nAcme Realty\n(555) 111-2222",  # signature block
    "> On Tue you wrote...\n> new listing\nRemove me from this list please.",  # quoted history
    "quit sending me these",
    "I deal direct only, no brokers. Remove me.",
    "take me off pls",
]


class BrokerOptOutSuppressionTest(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFirestore()
        self._patch = mock.patch.object(P, "_fs", self.fs)
        self._patch.start()
        # Record the opt-out through the REAL store fn so we exercise the real
        # hashing / normalization on the way in.
        ok = P._store_contact_optout(USER, "Dana.Broker@Acme-Realty.com", "unsubscribe", THREAD)
        self.assertTrue(ok, "store_contact_optout should succeed with faked Firestore")

    def tearDown(self):
        self._patch.stop()

    # ---- is_contact_opted_out: FIRES on every messy form (no false negative) --
    def test_guard_fires_on_all_messy_address_forms(self):
        for label, form in REAL_THREAT_FORMS:
            with self.subTest(form=label):
                rec = P.is_contact_opted_out(USER, form)
                self.assertIsNotNone(
                    rec,
                    f"FALSE NEGATIVE: opted-out broker not recognized via form '{label}' "
                    f"({form!r}) -> a send would go through to a broker who opted out.",
                )
                self.assertEqual(rec.get("reason"), "unsubscribe")

    # ---- is_contact_opted_out: does NOT fire on near-miss addresses -----------
    def test_guard_silent_on_near_miss_addresses(self):
        for label, addr in NEAR_MISS_ADDRESSES:
            with self.subTest(addr=label):
                rec = P.is_contact_opted_out(USER, addr)
                self.assertIsNone(
                    rec,
                    f"FALSE POSITIVE: near-miss address '{label}' ({addr!r}) treated as "
                    f"opted-out -> would block a legitimate, different broker.",
                )

    # ---- stopIf: blocked address reappears through CC -------------------------
    def _sent_addresses(self, result):
        addrs = set(result.get("sentRecipients") or [])
        for key in ("toRecipients", "ccRecipients"):
            for r in result["payload"].get(key, []):
                addrs.add(((r or {}).get("emailAddress") or {}).get("address"))
        return {a for a in addrs if a}

    def test_optout_dropped_from_cc_across_forms(self):
        for label, form in REAL_THREAT_FORMS:
            with self.subTest(form=label):
                draft = {
                    "toRecipients": [
                        {"emailAddress": {"address": "coordinator@acme-realty.com"}}
                    ],
                    "ccRecipients": [
                        {"emailAddress": {"address": form, "name": "Dana Broker"}}
                    ],
                }
                result = E._filter_reply_all_draft_recipients(
                    USER, draft, user_email="agent@sitesift.com"
                )
                sent = self._sent_addresses(result)
                self.assertNotIn(
                    OPTED_OUT,
                    sent,
                    f"FALSE NEGATIVE (CC reappearance): opted-out broker via form "
                    f"'{label}' ({form!r}) survived reply-all filtering -> {sent}.",
                )

    def test_optout_dropped_when_present_in_both_to_and_cc(self):
        draft = {
            "toRecipients": [{"emailAddress": {"address": OPTED_OUT}}],
            "ccRecipients": [{"emailAddress": {"address": "DANA.BROKER@acme-realty.com"}}],
        }
        result = E._filter_reply_all_draft_recipients(
            USER, draft, user_email="agent@sitesift.com"
        )
        self.assertNotIn(OPTED_OUT, self._sent_addresses(result))

    def test_near_miss_addresses_survive_cc_filter(self):
        # A legit different broker in CC (e.g. the wrong_contact redirect target)
        # must NOT be dropped by the opt-out guard.
        draft = {
            "toRecipients": [{"emailAddress": {"address": "coordinator@acme-realty.com"}}],
            "ccRecipients": [
                {"emailAddress": {"address": addr}} for _, addr in NEAR_MISS_ADDRESSES
            ],
        }
        result = E._filter_reply_all_draft_recipients(
            USER, draft, user_email="agent@sitesift.com"
        )
        sent = self._sent_addresses(result)
        for label, addr in NEAR_MISS_ADDRESSES:
            with self.subTest(addr=label):
                self.assertIn(
                    addr,
                    sent,
                    f"FALSE POSITIVE: near-miss '{label}' ({addr!r}) was dropped by the "
                    f"opt-out CC filter -> blocked a legitimate broker.",
                )

    # ---- SAFETY BUG: guard FAILS OPEN on a transient Firestore error ----------
    def test_cc_filter_fails_closed_on_firestore_error(self):
        """
        stopIf: 'blocked address reappears through CC'.

        is_contact_opted_out() swallows ANY exception and returns None
        (processing.py ~2337). The reply-all CC filter (email.py ~890) calls it
        with no fail-closed wrapper, so a transient Firestore read error makes
        the guard silently PASS and the opted-out broker gets CC'd.

        followup.py DID harden this exact call (fails closed on exception),
        proving the intended posture is fail-CLOSED. This asserts the correct
        (fail-closed) behavior, so it goes RED against current code.
        """
        self.fs.raise_all_get = True
        draft = {
            "toRecipients": [{"emailAddress": {"address": "coordinator@acme-realty.com"}}],
            "ccRecipients": [{"emailAddress": {"address": OPTED_OUT, "name": "Dana Broker"}}],
        }
        result = E._filter_reply_all_draft_recipients(
            USER, draft, user_email="agent@sitesift.com"
        )
        sent = self._sent_addresses(result)
        self.assertNotIn(
            OPTED_OUT,
            sent,
            "SAFETY (fail-open): during a Firestore read error the opt-out guard "
            "let a known opted-out broker back into the CC recipients.",
        )

    def test_is_contact_opted_out_does_not_report_sendable_on_error(self):
        """
        Direct view of the same fail-open defect: a broker who IS on the
        opted-out list must never be reported 'not opted out' just because the
        backing store hiccuped. Returning None here == 'safe to send' to callers
        (send_and_index / multi-property), which is a re-send to a blocked
        address (stopIf 'follow-up sends after opt-out'). RED against current code.
        """
        self.fs.raise_all_get = True
        rec = P.is_contact_opted_out(USER, OPTED_OUT)
        self.assertIsNotNone(
            rec,
            "SAFETY (fail-open): opt-out lookup errored and returned None, which "
            "every non-followup send path reads as 'not opted out' -> send proceeds "
            "to an opted-out broker.",
        )


class DeterministicDetectionCharacterizationTest(unittest.TestCase):
    """
    Detection is LLM-only. These tests characterize the deterministic
    augmentation so a future regression that starts silently mis-handling
    opt-out language at the deterministic layer would be caught.
    """

    def _augment(self, text, seed_events=None):
        proposal = {"events": list(seed_events or [])}
        convo = [{"direction": "inbound", "content": text}]
        return A._augment_events_with_deterministic_signals(proposal, convo)

    def test_optout_language_not_deterministically_reclassified(self):
        # No opt-out phrasing should be turned into a terminal property_unavailable
        # or tour_requested by the deterministic layer (which has no opt-out branch).
        for text in OPT_OUT_PHRASINGS:
            with self.subTest(text=text[:40]):
                out = self._augment(text, seed_events=[])
                types = {(e or {}).get("type") for e in out.get("events", [])}
                self.assertNotIn(
                    "property_unavailable",
                    types,
                    f"Opt-out phrasing {text[:40]!r} was deterministically coerced to "
                    f"property_unavailable.",
                )
                # Documents the LLM-only gap: deterministic layer never adds opt-out.
                self.assertNotIn("contact_optout", types)

    def test_near_miss_offer_another_listing_not_terminal(self):
        # Near-miss: "not this property but here's another listing" is NOT a global
        # opt-out and must not be deterministically collapsed to property_unavailable
        # when a replacement new_property is present.
        text = "That space isn't a fit, but I have another listing at 200 Main St."
        out = self._augment(text, seed_events=[{"type": "new_property", "reason": "alt"}])
        types = [(e or {}).get("type") for e in out.get("events", [])]
        self.assertIn("new_property", types)
        self.assertNotIn("contact_optout", types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
