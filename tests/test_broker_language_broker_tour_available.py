"""Pressure test: broker_tour_available -> outbound tour-scheduling SAFETY guard.

EVENT: broker_tour_available. A broker offers/commits to a tour ("happy to
schedule a tour", "we can show it Friday", "walk the space"). The SAFETY concern
(stopIf) is the mirror image: the user's own NORMAL campaign / core follow-up
automation must never emit tour-scheduling language, and tour language must not
leak into core follow-up.

The deterministic guard for that is:
    outbound_safety.contains_unreviewed_scheduling_language(body) -> bool
    outbound_safety.validate_outbound_body(body)  (is_safe=False when it fires)

It is enforced with NO allowance on the core follow-up lane
(followup.py:615 -> validate_outbound_body(followup_message)) and on
pending_responses.py:203 / processing.py:2515. Its own docstring: "Return True
when normal outreach copy drifts into tour/LOI scheduling."

This test drives the REAL function on realistic outbound copy that has drifted
into tour-scheduling language. It asserts the CORRECT safety behavior (guard
FIRES on tour commitments; stays quiet on near-misses). Assertions that pin
current WRONG behavior are left RED and reported as bugs. No source is modified;
no Firestore/Sheets/Graph is touched (the guard is pure string logic).
"""

import unittest

from email_automation.outbound_safety import (
    contains_unreviewed_scheduling_language,
    validate_outbound_body,
)


# Realistic outbound copy that has DRIFTED into offering/committing to a tour.
# These are exactly the shapes a broker uses (seeds) and that an LLM-generated
# core follow-up could echo back. Every one of these should be BLOCKED before
# it is sent on the normal lane.
REAL_THREAT_PHRASINGS = [
    # --- seeds ---
    "Happy to schedule a tour next week.",
    "We can show it Friday afternoon.",
    "Let me know when your client wants to walk the space.",
    # --- explicit scheduling verb + tour/showing noun (unambiguous) ---
    "I can arrange a tour for your client Tuesday.",
    "Would you like to schedule a tour?",
    "Let's set up a showing this week.",
    "We are happy to book a tour whenever works for you.",
    "I'll go ahead and schedule the tour for Thursday at 2pm.",
    # --- terse ---
    "Tour Fri 2pm?",
    "Can do a showing Mon.",
    # --- ALL CAPS ---
    "HAPPY TO SET UP A TOUR THIS WEEK, JUST SAY WHEN.",
    # --- typo'd ---
    "Happy to shedule a tuor next week if that helps.",
    # --- regional / synonym ---
    "Keen to arrange a viewing of the unit at your convenience.",
    "We can do a walkthrough Thursday morning.",
    # --- verbose + signature block ---
    (
        "Hi there,\n\nThanks so much for the continued interest in the property. "
        "To keep things moving, I'd be glad to schedule a tour for you and your "
        "client sometime next week -- mornings tend to work best on our end.\n\n"
        "Best,\nJordan Blake\nSenior Associate\nApex Commercial\n(312) 555-0142"
    ),
    # --- quoted history (broker's tour offer quoted back into our outbound) ---
    (
        "Following up on the note below.\n\n"
        "> On Tue, the broker wrote: Happy to schedule a tour next week, just "
        "let me know a day that works.\n\n"
        "Let's lock in a time to see the space."
    ),
    # --- conflicting-with-old-quote / multi-intent ---
    (
        "The asking rate is still $32/SF as quoted. Separately, I'd love to show "
        "you the space -- can we set up a tour for Wednesday?"
    ),
    "Come see the space anytime this week; I'll set up the showing.",
]


# Near-misses: these are LEGIT core-lane copy and MUST NOT be blocked.
NEAR_MISS_CONTROLS = [
    "Broker says tours are not possible yet.",
    "Normal Production V1 core lane should record interest, not schedule.",
    # legit interest-recording follow-up (no tour commitment)
    "Just following up to confirm the space is still available and note our interest.",
    "Could you share the current asking rate and available square footage?",
    "Thanks for the flyer -- we'll review internally and circle back.",
]


class BrokerTourAvailableGuardFiresOnThreats(unittest.TestCase):
    """The safety guard MUST fire on outbound copy that drifts into tour scheduling."""

    def test_guard_fires_on_every_tour_commitment(self):
        misses = [p for p in REAL_THREAT_PHRASINGS
                  if not contains_unreviewed_scheduling_language(p)]
        self.assertEqual(
            [], misses,
            "FALSE NEGATIVE: contains_unreviewed_scheduling_language did NOT fire on "
            "outbound copy that commits to / offers a tour. This tour language would "
            "leak into the core follow-up lane (followup.py:615 blocks nothing here). "
            f"Uncaught phrasings ({len(misses)}):\n  - "
            + "\n  - ".join(repr(m) for m in misses),
        )

    def test_validate_outbound_body_blocks_tour_commitments(self):
        """The user-facing gate must mark these unsafe (is_safe=False) on the core lane."""
        leaked = [p for p in REAL_THREAT_PHRASINGS
                  if validate_outbound_body(p).is_safe]
        self.assertEqual(
            [], leaked,
            "SAFETY HOLE: validate_outbound_body() returned is_safe=True for outbound "
            "copy offering a tour; the core follow-up / campaign lane would SEND it. "
            f"Would-send phrasings ({len(leaked)}):\n  - "
            + "\n  - ".join(repr(m) for m in leaked),
        )


class BrokerTourAvailableGuardQuietOnNearMisses(unittest.TestCase):
    """The safety guard MUST NOT fire on legit interest-recording copy."""

    def test_guard_silent_on_near_misses(self):
        false_positives = [p for p in NEAR_MISS_CONTROLS
                           if contains_unreviewed_scheduling_language(p)]
        self.assertEqual(
            [], false_positives,
            "FALSE POSITIVE: guard fired on legit core-lane copy, blocking a valid "
            f"broker email:\n  - " + "\n  - ".join(repr(p) for p in false_positives),
        )

    def test_validate_outbound_body_passes_near_misses(self):
        blocked = [p for p in NEAR_MISS_CONTROLS
                   if not validate_outbound_body(p).is_safe]
        self.assertEqual(
            [], blocked,
            "FALSE POSITIVE: validate_outbound_body blocked legit copy:\n  - "
            + "\n  - ".join(repr(p) for p in blocked),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
