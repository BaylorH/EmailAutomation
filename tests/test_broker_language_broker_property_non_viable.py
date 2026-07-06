"""Pressure test for broker_property_non_viable event class.

Deterministic guard under test:
    email_automation.ai_processing._augment_events_with_deterministic_signals
    (backed by _looks_like_requirements_mismatch_nonviable)

This is the safety BACKSTOP that emits a `property_unavailable` event when a
broker says the current target property fails the client's physical
requirements (office-heavy, not a true warehouse, no drive-in doors, clear
height below spec, etc.). We simulate the LLM MISSING the signal (empty
`events` proposal) and assert the deterministic guard catches the real-threat
phrasings and does NOT fire on the near-misses.

No Firestore / Sheets / Graph calls happen in these functions; both are pure
text/regex over the passed conversation, so nothing external needs patching.
Assertions pin the CORRECT behavior; where the current guard is wrong the test
stays RED to flag the bug (do not weaken).
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing as ai


def _fires(text: str) -> bool:
    """Drive the real deterministic guard on a single inbound broker message."""
    proposal = {"events": []}
    conversation = [{"direction": "inbound", "content": text}]
    out = ai._augment_events_with_deterministic_signals(proposal, conversation)
    return any(
        (event or {}).get("type") == "property_unavailable"
        for event in out.get("events", [])
    )


SIGNATURE = (
    "\n\n--\nDana Brooks\nSenior Associate | Industrial Advisory\n"
    "Cushwake Commercial | (312) 555-0192\n"
)


# ---------------------------------------------------------------------------
# REAL THREATS — broker declares the property non-viable; guard MUST fire.
# ---------------------------------------------------------------------------
REAL_THREATS = [
    # 0. seed 1 (fires today — positive control)
    "This is more office-heavy and not a true warehouse fit.",
    # 1. seed 2
    "We do not have drive-in doors, so it likely will not work.",
    # 2. seed 3
    "Clear height is only 14', below your requirement.",
    # 3. terse single clearest phrase
    "Not a true warehouse fit for your client.",
    # 4. ALL CAPS
    "THIS IS MORE OFFICE-HEAVY AND NOT A TRUE WAREHOUSE FIT.",
    # 5. explicit fit rejection + reason
    "This space won't be a good fit for your client - it's mostly office.",
    # 6. requirements language + clearance
    "It does not meet your client's requirements; ceiling clearance is far below spec.",
    # 7. lacks warehouse (single reason)
    "The suite lacks sufficient warehouse space for what they need.",
    # 8. verbose multi-intent (fires today)
    ("Thanks for reaching out. After reviewing the specs I don't think this one "
     "works - it's primarily office with no drive-in doors, so not a true warehouse."),
    # 9. clear height under spec, spelled out
    "Clear height is only 14 feet which is under the 24' your client requires.",
    # 10. no grade-level access (single reason)
    "The building doesn't have any grade-level access for the client.",
    # 11. contraction verb form
    "We don't have any drive-in or grade-level access here.",
    # 12. regional / casual
    "Nah, it's all office fit-out, no proper warehouse to speak of.",
    # 13. typo'd
    "Sorry, this one's not a true wharehouse - way too office heavy.",
    # 14. with signature block appended
    "This is more office-heavy and not a true warehouse fit." + SIGNATURE,
    # 15. partial / clipped
    "too office-heavy, not a warehouse - won't work for them",
]

# Phrasings that fire today (documented positive controls that should pass green).
_EXPECTED_GREEN = {0, 4, 8, 14}


# ---------------------------------------------------------------------------
# NEAR MISSES — guard MUST NOT fire.
# ---------------------------------------------------------------------------
NEAR_MISSES = [
    # a. clarifying question before judging fit
    ("Before I can judge fit, could you confirm the required clear height and "
     "how many drive-in doors the client needs?"),
    # b. quoted OLD rejection appears below a NEW positive reply
    ("Good news - after re-checking, this space actually works for your client. "
     "Ignore my earlier note.\n\n> On Jul 1 I wrote: this is more office-heavy "
     "and not a true warehouse fit"),
    # c. positive confirmation that merely mentions warehouse/drive-in specs
    ("Confirmed - it's a true warehouse with 24' clear and two drive-in doors, "
     "great fit for your client."),
]


class TestBrokerPropertyNonViableRealThreats(unittest.TestCase):
    """Every real non-viable phrasing must emit property_unavailable."""

    def test_real_threats_fire(self):
        misses = []
        for idx, text in enumerate(REAL_THREATS):
            if not _fires(text):
                misses.append((idx, text))
        self.assertEqual(
            misses,
            [],
            "Deterministic non-viable guard MISSED real-threat phrasings "
            "(false negatives / safety holes):\n"
            + "\n".join(f"  [{i}] {t!r}" for i, t in misses),
        )


class TestBrokerPropertyNonViableNearMisses(unittest.TestCase):
    """Near-misses must NOT emit property_unavailable."""

    def test_near_misses_do_not_fire(self):
        false_positives = []
        for idx, text in enumerate(NEAR_MISSES):
            if _fires(text):
                false_positives.append((idx, text))
        self.assertEqual(
            false_positives,
            [],
            "Deterministic non-viable guard FIRED on near-miss phrasings "
            "(false positives / blocks legit broker email):\n"
            + "\n".join(f"  [{i}] {t!r}" for i, t in false_positives),
        )


class TestKnownGreenControls(unittest.TestCase):
    """Sanity: the phrasings that DO work today keep working (regression guard)."""

    def test_known_positive_controls_fire(self):
        for idx in sorted(_EXPECTED_GREEN):
            self.assertTrue(
                _fires(REAL_THREATS[idx]),
                f"Regression: control [{idx}] {REAL_THREATS[idx]!r} stopped firing",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
