"""
Pressure test for the broker_property_unavailable event class.

Deterministic guard under test:
    email_automation.ai_processing._augment_events_with_deterministic_signals

This is the deterministic backstop that adds a `property_unavailable` event when
the broker's latest inbound message says the target property is leased / off-market /
no longer available. It exists precisely to catch what "the model can miss"
(docstring: "Add high-confidence event signals from broker phrases the model can miss.").

The False-Positive guard is looks_like_tour_only_unavailable: tour/showing-only
unavailability must NOT be promoted to property_unavailable.

We drive the REAL function on many realistic phrasings. No Firestore / Sheets / Graph
calls happen — the function is pure over (proposal dict, conversation list).

Assertions that reflect WRONG current behavior are written to assert the CORRECT
behavior, so they FAIL (red) and pin the bug. They are NOT weakened to force green.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.ai_processing import _augment_events_with_deterministic_signals


def _conv(text):
    return [{"direction": "inbound", "content": text}]


def fires_property_unavailable(text, seed_events=None):
    """Run the REAL deterministic guard; return True if a property_unavailable event results."""
    proposal = {"events": list(seed_events or [])}
    result = _augment_events_with_deterministic_signals(proposal, _conv(text))
    return any((e or {}).get("type") == "property_unavailable" for e in (result.get("events") or []))


# ---------------------------------------------------------------------------
# Real-threat phrasings the guard DOES catch today (true positives — should be green)
# ---------------------------------------------------------------------------
TRUE_POSITIVE_CAUGHT = [
    "That suite is no longer available.",                                   # seed 1
    "The building is fully leased.",
    "We signed an LOI on that space.",
    "We signed a lease last week, sorry.",
    "No space available in that property right now.",
    "We have no availability at this time.",
    "I no longer represent this property.",
    "THAT SUITE IS NO LONGER AVAILABLE.",                                   # ALL CAPS
    "Hi there,\n\nThat suite is no longer available.\n\nBest,\nJohn Doe\nAcme Realty | (555) 010-2020",  # signature block
    "Following up on your inquiry — unfortunately that suite is no longer available as of last week.\n\n> On Mon you wrote: is the suite still open?",  # quoted history
    "This space isn't a good fit for your client; it's more office-heavy than a true warehouse.",  # requirements_mismatch
]

# ---------------------------------------------------------------------------
# Real-threat phrasings the guard MISSES today (false negatives — SHOULD fire).
# These assertions are written CORRECT and will be RED until the guard is fixed.
# Each maps to a documented unavailable signal already present in
# processing.PROPERTY_UNAVAILABLE_KEYWORDS, proving the intent that they ARE unavailable.
# ---------------------------------------------------------------------------
FALSE_NEGATIVE_MISSED = [
    "We just leased the building.",                    # seed 2  (kw: "just leased")
    "Property is off market for now.",                 # seed 3  (kw: "off market")
    "The space is under contract.",                    #         (kw: "under contract")
    "That property has been leased already.",          #         (kw: "already leased")
    "It's been leased.",                               #         (kw: "leased")
    "The property's been taken off the market.",       #         (kw: "taken off market")
    "We accepted an offer on that space.",             #         (kw: "accepted an offer")
]

# Typo variant — lower severity but still a genuine backstop gap.
FALSE_NEGATIVE_TYPO = "That suite is no longer availabe."  # single-char typo of "available"

# ---------------------------------------------------------------------------
# Near-misses — MUST NOT be promoted to property_unavailable (false-positive guard).
# ---------------------------------------------------------------------------
NEAR_MISSES = [
    "Tours are unavailable but the property remains available.",
    "One suite is leased but an alternate suite in the same property remains viable.",
    "The suite is no longer available for tours this week.",     # tour-only phrased with the hard trigger words
    "Showings are unavailable right now, but the listing is still active.",
]


class TruePositivesDoFire(unittest.TestCase):
    """These SHOULD fire and DO fire today — pins that we did not weaken the guard."""

    def test_caught_phrasings_fire(self):
        for text in TRUE_POSITIVE_CAUGHT:
            with self.subTest(text=text):
                self.assertTrue(
                    fires_property_unavailable(text),
                    msg=f"Expected property_unavailable to fire for real-threat phrasing: {text!r}",
                )


class NearMissesDoNotFire(unittest.TestCase):
    """False-positive guard: tour/showing-only unavailability must NOT mark the property unavailable."""

    def test_near_misses_do_not_fire(self):
        for text in NEAR_MISSES:
            with self.subTest(text=text):
                self.assertFalse(
                    fires_property_unavailable(text),
                    msg=f"FALSE POSITIVE: near-miss wrongly marked property_unavailable: {text!r}",
                )


class FalseNegativesShouldFire(unittest.TestCase):
    """
    RED. These are common, unambiguous 'property is gone' phrasings. The deterministic
    backstop should catch them (the model can miss them), but it does NOT. Each phrasing
    corresponds to a keyword already in processing.PROPERTY_UNAVAILABLE_KEYWORDS, so the
    codebase already treats them as terminal — yet no property_unavailable event is created,
    so the thread never terminalizes and follow-ups keep sending to a dead property.
    """

    def test_missed_real_threats_should_fire(self):
        failures = []
        for text in FALSE_NEGATIVE_MISSED:
            if not fires_property_unavailable(text):
                failures.append(text)
        self.assertEqual(
            failures,
            [],
            msg=(
                "FALSE NEGATIVE (safety hole): deterministic guard did NOT emit "
                "property_unavailable for these real-threat phrasings:\n  - "
                + "\n  - ".join(repr(t) for t in failures)
            ),
        )

    def test_typo_variant_should_fire(self):
        self.assertTrue(
            fires_property_unavailable(FALSE_NEGATIVE_TYPO),
            msg=f"FALSE NEGATIVE (typo): guard missed {FALSE_NEGATIVE_TYPO!r}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
