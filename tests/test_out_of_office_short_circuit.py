"""Mentioning a holiday must not switch off the rest of the classifier.

The out-of-office guard exists for a good reason (LIVE breaks E1/E3): an OOO
auto-reply that names a backup contact is not an intentional handoff, and the model
kept escalating the wrong person. It strips that and returns.

The early return is the problem. It fires on ANY message containing an OOO-ish
phrase, and a real broker apologising for a slow reply says exactly those words:

    "Thanks for the follow up Jill. Was on vacation last week, I'm sorry for the
     delay! It is available and should be a good fit. 7000 sf total $0.90/sf NNN
     $0.20/sf Opex"

That is a substantive reply carrying three property facts, and it was found in real
customer traffic. Because the guard returned early, every deterministic check after
it was skipped for that message -- so an apology of that shape can silently swallow
a call request the broker made in the same breath, and can defeat the deferral
silence guard.

The stripping is kept. The short circuit is now limited to a message that really is
just an auto-reply; anything carrying content of its own is stripped AND then
reasoned about normally.
"""
import os
import sys
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation.ai_processing import (  # noqa: E402
    _augment_events_with_deterministic_signals,
    _looks_like_out_of_office,
    _is_bare_auto_reply,
)

REASK = ("Hi,\n\nThank you for the information!\n\nTo complete the property details, "
         "could you please provide:\n\n- Ops Ex / SF")

BARE_OOO = "Automatic reply: I am out of the office until Monday with limited email access."
REAL_REPLY_WITH_CALL = (
    "Thanks for the follow up. Was on vacation last week, sorry for the delay! "
    "It is available and should be a good fit. 7000 sf total, $0.90/sf NNN. "
    "Let's hop on a quick call to go through it."
)
REAL_REPLY_WITH_DEFERRAL = (
    "Sorry, was on vacation last week. It is available, 7000 sf total. "
    "Opex is still pending, I will send it next."
)


def _conv(text):
    return [{"direction": "inbound", "from": "broker@example-cre.com", "content": text}]


def _proposal():
    return {"updates": [], "events": [], "response_email": REASK}


class BareAutoReplyDetectionTests(unittest.TestCase):
    def test_a_bare_auto_reply_is_recognised(self):
        self.assertTrue(_is_bare_auto_reply(BARE_OOO))

    def test_a_substantive_reply_is_not_a_bare_auto_reply(self):
        for text in (REAL_REPLY_WITH_CALL, REAL_REPLY_WITH_DEFERRAL):
            with self.subTest(text=text[:40]):
                self.assertFalse(_is_bare_auto_reply(text))

    def test_the_underlying_ooo_phrase_still_matches_both(self):
        """The phrase detector is unchanged; only the short circuit narrows."""
        for text in (BARE_OOO, REAL_REPLY_WITH_CALL, REAL_REPLY_WITH_DEFERRAL):
            with self.subTest(text=text[:40]):
                self.assertTrue(_looks_like_out_of_office(text))


class ShortCircuitTests(unittest.TestCase):
    def test_a_call_request_survives_an_apology_for_being_away(self):
        out = _augment_events_with_deterministic_signals(_proposal(), _conv(REAL_REPLY_WITH_CALL))
        self.assertTrue(
            any((e or {}).get("type") == "call_requested" for e in out.get("events") or []),
            "a call the broker asked for must still reach a human",
        )
        self.assertIsNone(out.get("response_email"))

    def test_a_deferral_still_silences_the_reask_after_an_apology(self):
        out = _augment_events_with_deterministic_signals(_proposal(), _conv(REAL_REPLY_WITH_DEFERRAL))
        self.assertIsNone(
            out.get("response_email"),
            "he said the number is coming; the apology for being away changes nothing",
        )

    def test_a_bare_auto_reply_still_strips_a_wrong_contact_escalation(self):
        """The original guard's purpose, unchanged."""
        proposal = {
            "updates": [], "response_email": REASK,
            "events": [{"type": "wrong_contact", "reason": "ooo_backup"}],
        }
        out = _augment_events_with_deterministic_signals(
            proposal,
            _conv("Automatic reply: I am out of the office. For urgent matters "
                  "contact my assistant at assistant@example-cre.com."),
        )
        self.assertFalse(
            any((e or {}).get("type") == "wrong_contact" for e in out.get("events") or []),
            "an OOO backup address is not an intentional handoff",
        )

    def test_a_substantive_reply_also_still_strips_the_wrong_contact(self):
        proposal = {
            "updates": [], "response_email": REASK,
            "events": [{"type": "wrong_contact", "reason": "ooo_backup"}],
        }
        out = _augment_events_with_deterministic_signals(proposal, _conv(REAL_REPLY_WITH_CALL))
        self.assertFalse(
            any((e or {}).get("type") == "wrong_contact" for e in out.get("events") or []),
        )


if __name__ == "__main__":
    unittest.main()
