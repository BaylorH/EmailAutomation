"""Deterministic engaged-alternative guard — found via LIVE testing on real code.

LIVE break B9: a broker reply that scopes disinterest to ONE suite while asking
to see alternatives ("I'm not interested in that particular suite, but show me
what else you have nearby.") classified as events=['contact_optout'],
escalated=True. That silently STOPS the thread even though the broker is an
active lead requesting more options.

The LLM scoped the phrase "not interested" to the whole contact instead of the
single suite. This guard deterministically strips contact_optout when the reply
both (a) scopes the rejection to a specific property/suite AND (b) requests
alternatives — while still preserving genuine opt-outs (unsubscribe / stop
emailing / remove me), which must NEVER be suppressed.
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
    _looks_like_engaged_alternative_request,
)

BROKER = "bp21harrison+edge-b9@gmail.com"
B9_BODY = (
    "Thanks for reaching out. I'm not interested in that particular suite, "
    "but show me what else you have nearby."
)


def _conv(body):
    return [{"direction": "inbound", "from": BROKER, "to": ["baylor.freelance@outlook.com"],
             "subject": "Re: Space availability", "timestamp": "2026-07-05T03:55:00Z", "content": body}]


class DetectEngagedAlternativeTests(unittest.TestCase):
    def test_scoped_rejection_plus_alternatives_request_fires(self):
        self.assertTrue(_looks_like_engaged_alternative_request(B9_BODY))

    def test_variants_fire(self):
        for body in [
            "Not interested in this space, but what else do you have in the area?",
            "That specific property isn't for us — send me other options nearby.",
            "Not interested in this one; got anything else available?",
            "This suite doesn't work, but show me the others you have.",
        ]:
            self.assertTrue(
                _looks_like_engaged_alternative_request(body),
                f"should fire for: {body!r}",
            )

    def test_plain_flat_optout_does_not_fire(self):
        # No alternatives request → not an engaged lead, leave the opt-out intact.
        self.assertFalse(_looks_like_engaged_alternative_request("Not interested, thanks."))
        self.assertFalse(_looks_like_engaged_alternative_request(
            "Please remove me from your mailing list."))

    def test_hard_optout_with_alternatives_phrase_does_not_fire(self):
        # A genuine opt-out must never be suppressed even if it mentions alternatives.
        body = ("I'm not interested in this property or anything else you have. "
                "Please stop emailing me and remove me from your list.")
        self.assertFalse(_looks_like_engaged_alternative_request(body))


class AugmentStripsOveredOptoutTests(unittest.TestCase):
    def test_b9_strips_contact_optout_and_unescalates(self):
        # Simulate the LLM's bad run: it over-fired contact_optout on an engaged lead.
        proposal = {
            "events": [{"type": "contact_optout", "reason": "not_interested"}],
            "response_email": None,
            "updates": [],
        }
        out = _augment_events_with_deterministic_signals(proposal, _conv(B9_BODY))
        types = [e.get("type") for e in out["events"]]
        self.assertNotIn("contact_optout", types,
                         "engaged lead asking for alternatives must NOT opt out the contact")

    def test_b9_preserves_other_events(self):
        proposal = {
            "events": [
                {"type": "contact_optout", "reason": "not_interested"},
                {"type": "needs_user_input", "reason": "client_question"},
            ],
            "response_email": None,
            "updates": [],
        }
        out = _augment_events_with_deterministic_signals(proposal, _conv(B9_BODY))
        types = [e.get("type") for e in out["events"]]
        self.assertNotIn("contact_optout", types)
        self.assertIn("needs_user_input", types)

    def test_genuine_optout_is_never_stripped(self):
        proposal = {
            "events": [{"type": "contact_optout", "reason": "unsubscribe"}],
            "response_email": None,
            "updates": [],
        }
        body = ("I'm not interested in this property or anything else you have. "
                "Please stop emailing me and remove me from your list.")
        out = _augment_events_with_deterministic_signals(proposal, _conv(body))
        types = [e.get("type") for e in out["events"]]
        self.assertIn("contact_optout", types,
                      "a genuine opt-out must survive the engaged-alternative guard")

    def test_no_optout_event_is_a_noop(self):
        proposal = {"events": [], "response_email": "Hi, sure — here are a few nearby.", "updates": []}
        out = _augment_events_with_deterministic_signals(proposal, _conv(B9_BODY))
        self.assertEqual([e.get("type") for e in out["events"]], [])
        self.assertIsNotNone(out["response_email"])


if __name__ == "__main__":
    unittest.main()
