"""Deterministic colleague-redirect guard — found via LIVE testing on real code.

A real multi-intent BP21 reply ("1200 Edge just leased, but try 4400 Referral
Way ..., and my colleague Dana Reyes (dana@example-cre.com) actually handles the
south submarket, loop her in") classified NONDETERMINISTICALLY: on some runs the
LLM dropped wrong_contact AND left a non-null response_email, so the system
auto-committed to looping in an unapproved third party instead of escalating.
This guard forces the wrong_contact escalation deterministically.
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
    _detect_colleague_redirect,
)

BROKER = "bp21harrison+edge-row03@gmail.com"
REDIRECT_BODY = (
    "Hi John, 1200 Edge Test Blvd was just leased last week so that's off the table. "
    "But I have another option - 4400 Referral Way, Austin: 32,000 SF, $9.25 NNN. "
    "Also my colleague Dana Reyes (dana@example-cre.com) actually handles the south "
    "submarket, loop her in on anything down there. Thanks, Sam"
)


def _conv(body):
    return [{"direction": "inbound", "from": BROKER, "to": ["baylor.freelance@outlook.com"],
             "subject": "Re: Space availability", "timestamp": "2026-07-05T03:55:00Z", "content": body}]


class DetectRedirectTests(unittest.TestCase):
    def test_detects_colleague_with_distinct_email(self):
        got = _detect_colleague_redirect(REDIRECT_BODY, BROKER)
        self.assertIsNotNone(got)
        self.assertEqual(got["suggestedEmail"], "dana@example-cre.com")
        self.assertEqual(got["suggestedContact"], "Dana Reyes")

    def test_no_redirect_phrase_returns_none(self):
        body = "Hi John, it's available - 32,000 SF, $9.25 NNN. Flyer attached. Thanks, Sam"
        self.assertIsNone(_detect_colleague_redirect(body, BROKER))

    def test_redirect_phrase_but_only_sender_email_does_not_fire(self):
        # broker restating their own address is not a redirect
        body = "You can loop in anyone; my address is " + BROKER + " for the record."
        self.assertIsNone(_detect_colleague_redirect(body, BROKER))


class AugmentEscalationTests(unittest.TestCase):
    def test_multi_intent_redirect_forces_wrong_contact_and_nulls_reply(self):
        # Simulate the LLM's "bad" run: new_property present, NO wrong_contact,
        # non-null response_email (the auto-commit failure).
        proposal = {
            "events": [{"type": "new_property", "address": "4400 Referral Way", "city": "Austin"}],
            "response_email": "Hi Sam, ... I'll loop her in for the south submarket.",
            "updates": [],
        }
        out = _augment_events_with_deterministic_signals(proposal, _conv(REDIRECT_BODY))
        types = [e.get("type") for e in out["events"]]
        self.assertIn("wrong_contact", types, "redirect must force a wrong_contact escalation")
        wc = next(e for e in out["events"] if e["type"] == "wrong_contact")
        self.assertEqual(wc["suggestedEmail"], "dana@example-cre.com")
        self.assertIsNone(out["response_email"], "a redirect must escalate, never auto-reply")
        # the legitimate new_property is preserved for operator approval
        self.assertIn("new_property", types)

    def test_no_double_wrong_contact_if_llm_already_fired_it(self):
        proposal = {
            "events": [{"type": "wrong_contact", "suggestedEmail": "dana@example-cre.com"}],
            "response_email": None, "updates": [],
        }
        out = _augment_events_with_deterministic_signals(proposal, _conv(REDIRECT_BODY))
        wc = [e for e in out["events"] if e.get("type") == "wrong_contact"]
        self.assertEqual(len(wc), 1)

    def test_plain_reply_no_false_escalation(self):
        proposal = {"events": [], "response_email": "Hi Sam, thanks - please send the flyer.", "updates": []}
        body = "Hi John, it's available - 32,000 SF, $9.25 NNN, 28' clear. Flyer attached. Thanks, Sam"
        out = _augment_events_with_deterministic_signals(proposal, _conv(body))
        self.assertNotIn("wrong_contact", [e.get("type") for e in out["events"]])
        self.assertIsNotNone(out["response_email"], "a normal reply must still auto-respond")


if __name__ == "__main__":
    unittest.main()
