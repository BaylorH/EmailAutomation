"""A deficiency the broker offers to FIX is not a dead property.

LIVE break (a real client's broker thread, 2026-07-18), and the client wrote in
about it directly. The requirement list included one drive-in door. The broker
replied:

    "No drive in door. 1 loading dock. The loading dock can be ramped for drive in
     The unit is 7753 sf. All information is in the flyer I sent to you."

That is a broker saying the requirement CAN be met. The classifier emitted
property_unavailable / requirements_mismatch, the row was terminalized, and the
broker was told the property was no longer available. She replied, flatly, "The
property IS available", and the client had to phone her and repair the spreadsheet
by hand, writing: "It 'detected property unavailable' when that was absolutely
incorrect, and there was nothing in the email that should have caused the AI to
think that."

The wording and the extraction faults from that thread were fixed on 2026-08-01 and
2026-08-07. This one was not: the code already detects the remediation offer -- it
uses it to protect the drive-in COLUMN value -- but nothing stops the terminal
EVENT, so a remediable non-fit still kills the lead.

Scoped to requirements_mismatch on purpose. A genuinely dead property is dead even
if a dock could theoretically be ramped, so "it's leased" is untouched.
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

import email_automation.ai_processing as A  # noqa: E402

LIVE = ("No drive in door. 1 loading dock. The loading dock can be ramped for drive in "
        "The unit is 7753 sf. All information is in the flyer I sent to you.")


def _conv(text):
    return [{"direction": "inbound", "from": "broker@example-cre.com", "content": text}]


def _proposal(reason="requirements_mismatch", updates=None):
    return {
        "updates": list(updates or []),
        "events": [{"type": "property_unavailable", "reason": reason}],
        "response_email": None,
    }


def _types(out):
    return [(e or {}).get("type") for e in out.get("events") or []]


class RemediableMismatchTests(unittest.TestCase):
    def test_the_live_message_does_not_terminalize_the_row(self):
        out = A._augment_events_with_deterministic_signals(_proposal(), _conv(LIVE))
        self.assertNotIn(
            "property_unavailable", _types(out),
            "the broker offered to ramp the dock; the property is available and the "
            "requirement can be met",
        )

    def test_the_facts_he_gave_are_still_written(self):
        updates = [{"column": "Total SF", "value": "7753"},
                   {"column": "Drive Ins", "value": "0"}]
        out = A._augment_events_with_deterministic_signals(_proposal(updates=updates), _conv(LIVE))
        self.assertEqual(out.get("updates"), updates)

    def test_remediation_phrase_family(self):
        for text in [
            "There is no drive-in, but the dock can be converted to a grade-level door.",
            "The opening can be modified to a drive-in if needed.",
            "No drive-in today, but the landlord can install one.",
        ]:
            with self.subTest(text=text[:44]):
                out = A._augment_events_with_deterministic_signals(_proposal(), _conv(text))
                self.assertNotIn("property_unavailable", _types(out))

    @unittest.expectedFailure
    def test_known_gap_remedy_in_a_separate_clause(self):
        """DOCUMENTED LIMITATION, not a passing case.

        _looks_like_access_remediation requires the access word and the remedy to
        sit in the SAME clause, so "No grade level access currently; the landlord
        will add a ramp" splits at the semicolon and neither half qualifies -- the
        first names the deficiency, the second the remedy.

        Left failing on purpose rather than deleted. Widening that detector reaches
        a second consumer (it also protects the drive-in column value), so it wants
        a deliberate change with its own evidence rather than a late-night widening
        on the back of one invented phrasing. This test turns green the day someone
        does it.
        """
        text = "No grade level access currently; the landlord will add a ramp."
        out = A._augment_events_with_deterministic_signals(_proposal(), _conv(text))
        self.assertNotIn("property_unavailable", _types(out))

    def test_a_genuine_terminal_survives_even_with_a_ramp_mentioned(self):
        """A leased property is dead however convertible its dock is."""
        text = ("That space has been leased as of last week. For reference the dock "
                "could have been ramped for drive-in.")
        out = A._augment_events_with_deterministic_signals(
            _proposal(reason="no_longer_available"), _conv(text)
        )
        self.assertIn("property_unavailable", _types(out))

    def test_a_real_mismatch_with_no_remedy_still_terminalizes(self):
        text = "Clear height is only 14 feet, which is below your requirement. No way around it."
        out = A._augment_events_with_deterministic_signals(_proposal(), _conv(text))
        self.assertIn("property_unavailable", _types(out))

    def test_an_explicitly_refused_remedy_still_terminalizes(self):
        text = "There is no drive-in door and the dock cannot be ramped for drive in."
        out = A._augment_events_with_deterministic_signals(_proposal(), _conv(text))
        self.assertIn("property_unavailable", _types(out))


if __name__ == "__main__":
    unittest.main()
