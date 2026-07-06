"""Pressure test for the broker_tour_unavailable event class.

SAFETY CONTRACT
---------------
When a broker says only *tours / showings* are unavailable (the property /
listing itself is still on the market, or the restriction is temporary, e.g.
"until the current tenant vacates"), the system MUST NOT mark the property as
stopped / non-viable, and MUST NOT keep pushing for a tour. The deterministic
guard for this is:

  - email_automation.tour_scheduling.looks_like_tour_only_unavailable(text)
        -> classifies a broker message as "tours-only unavailable". This is the
           GATE (ai_processing._augment_events_with_deterministic_signals line
           ~180) that PREVENTS the property_unavailable branch from firing, and
           the trigger (line ~208) that scrubs a stray property_unavailable
           event and re-emits tour_requested.

  - email_automation.ai_processing._augment_events_with_deterministic_signals(
        proposal, conversation)
        -> the full deterministic reconciliation over the LLM proposal.

A tours-only phrasing the detector MISSES is a false negative: the message
falls through to the property_unavailable pattern matcher, which can mark the
whole property non-viable (violates stopIf "property is marked stopped or
non-viable"). We assert the CORRECT behavior; misses stay RED and are reported
as bugs. No source is modified. No Firestore / Sheets / Graph is touched (the
functions under test are pure).
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation.ai_processing import _augment_events_with_deterministic_signals
from email_automation.tour_scheduling import looks_like_tour_only_unavailable


# Real broker phrasings that a human plainly reads as "tours/showings are
# unavailable, but the PROPERTY is still available / the block is temporary".
# Every one of these MUST classify as tours-only-unavailable.
TOUR_UNAVAILABLE_REAL = [
    # --- seeds ---
    "Tours are not available this week, but the space is still available.",
    "We cannot show it until the current tenant vacates.",
    "No tours right now; I can send photos.",
    # --- terse / partial ---
    "We can't do tours at the moment.",
    "No tour availability at this time.",
    "No tours currently; happy to share the flyer and photos.",
    # --- verbose / conditional ---
    "No showings until the tenant moves out, but the unit is still on the market.",
    "We are unable to schedule tours until the current tenant vacates.",
    "The property is not available for tours right now, but it's still on the market.",
    "We won't be able to arrange a tour this month.",
    # --- regional / synonym ---
    "Showings are unavailable until further notice.",
    "Walkthroughs are not being offered currently.",
    "Tours have been suspended for now; the listing is active.",
    "Can't show the space right now but it remains available.",
    # --- typo'd / contraction ---
    "The space isn't available to tour this week.",
    # --- ALL CAPS ---
    "TOURS NOT AVAILABLE THIS WEEK — SPACE STILL OPEN.",
    # --- 'no availability' phrased against showings (also trips the
    #     property_unavailable 'no_availability' pattern -> active harm) ---
    "No availability to show the space this week, but it's still listed.",
    # --- with a signature block ---
    (
        "Hi there,\n\nUnfortunately no tours are available right now while the "
        "current tenant is still in place, but the suite is very much still on "
        "the market.\n\nBest,\nDana Ruiz\nAcme CRE | (312) 555-0100"
    ),
    # --- quoted history + conflicting old quote ---
    (
        "Tours aren't available at the moment.\n\n> On Mon you wrote: Can you "
        "confirm a tour date and requested arrival time?\n> Asking rate was "
        "$12.50/SF/yr last quarter."
    ),
]

# Near-misses: these must NOT be treated as tours-only-unavailable.
#  - a genuinely unavailable PROPERTY (should stay property_unavailable)
#  - a broker rejecting one time but offering another (legit tour negotiation)
NEAR_MISS_NOT_TOUR_ONLY = [
    "Property itself is unavailable.",
    "That time doesn't work for me, but I could do Thursday at 3pm instead.",
]


def _thread(broker_msg):
    """Conversation with real tour-scheduling context + the broker reply."""
    return [
        {
            "direction": "outbound",
            "content": "Can you confirm a tour date and requested arrival time?",
        },
        {"direction": "inbound", "content": broker_msg},
    ]


def _event_types(proposal):
    return [(e or {}).get("type") for e in (proposal or {}).get("events", [])]


class TestTourOnlyDetector(unittest.TestCase):
    """Direct assertions on the core deterministic guard."""

    def test_real_threats_classify_as_tour_only(self):
        for msg in TOUR_UNAVAILABLE_REAL:
            with self.subTest(msg=msg):
                self.assertTrue(
                    looks_like_tour_only_unavailable(msg),
                    "FALSE NEGATIVE: broker restricted only tours/showings "
                    "(property still available) but the guard did not recognize "
                    "it as tours-only; message can fall through to the "
                    f"property_unavailable matcher. Input: {msg!r}",
                )

    def test_near_misses_are_not_tour_only(self):
        for msg in NEAR_MISS_NOT_TOUR_ONLY:
            with self.subTest(msg=msg):
                self.assertFalse(
                    looks_like_tour_only_unavailable(msg),
                    f"FALSE POSITIVE: near-miss treated as tours-only. Input: {msg!r}",
                )


class TestGuardDoesNotStopViableProperty(unittest.TestCase):
    """Full-guard reconciliation: a tours-only reply must never leave the
    property marked non-viable."""

    def test_property_not_marked_unavailable_on_tour_only_reply(self):
        # Simulates the LLM (understandably) mislabeling a tours-only reply as
        # property_unavailable. The deterministic guard must scrub it.
        for msg in TOUR_UNAVAILABLE_REAL:
            with self.subTest(msg=msg):
                proposal = {"events": [{"type": "property_unavailable", "reason": "x"}]}
                out = _augment_events_with_deterministic_signals(proposal, _thread(msg))
                self.assertNotIn(
                    "property_unavailable",
                    _event_types(out),
                    "SAFETY: tours-only reply left the property marked "
                    f"non-viable (stopIf violated). Input: {msg!r}",
                )

    def test_guard_never_invents_property_unavailable_from_tour_restriction(self):
        # LLM proposed NOTHING. The deterministic guard must not, on its own,
        # invent a property_unavailable event from a tours-only message.
        for msg in TOUR_UNAVAILABLE_REAL:
            with self.subTest(msg=msg):
                proposal = {"events": []}
                out = _augment_events_with_deterministic_signals(proposal, _thread(msg))
                self.assertNotIn(
                    "property_unavailable",
                    _event_types(out),
                    "SAFETY: guard invented property_unavailable from a "
                    f"tours-only broker message. Input: {msg!r}",
                )


class TestNearMissControls(unittest.TestCase):
    """Legit broker email must not be broken by the tour-unavailable path."""

    def test_genuinely_unavailable_property_stays_unavailable(self):
        msg = "Property itself is unavailable."
        proposal = {"events": [{"type": "property_unavailable", "reason": "x"}]}
        out = _augment_events_with_deterministic_signals(proposal, _thread(msg))
        types = _event_types(out)
        self.assertIn("property_unavailable", types)
        self.assertNotIn("tour_requested", types)

    def test_alternate_time_offer_is_not_property_unavailable(self):
        # Broker rejects one time, offers another -> a tour negotiation, never
        # a property stop.
        msg = "That time doesn't work for me, but I could do Thursday at 3pm instead."
        proposal = {"events": [{"type": "tour_requested", "reason": "y"}]}
        out = _augment_events_with_deterministic_signals(proposal, _thread(msg))
        self.assertNotIn("property_unavailable", _event_types(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
