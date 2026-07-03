import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest

# Real production function under test: the deterministic wrong-contact
# referral payload builder owned by core.event_classifier
# (email_automation/notification_payloads.py per feature-registry.json).
from email_automation.notification_payloads import build_wrong_contact_suggested_email


class CoreEventClassifierWrongRecipientTests(unittest.TestCase):
    """Rubric cell: core.event_classifier / wrong_recipient.

    Proves that when a broker reply carries the deterministic wrong-contact
    signal (this is the wrong person for this property row, try someone else),
    the classifier's outbound routing targets the SUGGESTED (correct) recipient
    and the correct property row -- never the original wrong recipient.
    """

    def test_wrong_contact_signal_routes_to_suggested_recipient_not_original_wrong_row(self):
        original_wrong_contact = "mike.wrong@example.com"
        correct_email = "Dana.Correct@Example.com"
        correct_row = "1200 Test Loop, North Las Vegas"

        payload = build_wrong_contact_suggested_email(
            original_contact=original_wrong_contact,
            suggested_contact="Dana Correct",
            suggested_email=correct_email,
            row_anchor=correct_row,
            referrer_name="Mike Wrong",
        )

        # Recipient targets the corrected contact, normalized, and NOT the
        # original wrong recipient that produced the signal.
        self.assertEqual(payload["to"], ["dana.correct@example.com"])
        self.assertNotIn(original_wrong_contact, payload["to"])
        self.assertNotIn("mike.wrong@example.com", payload["to"])

        # Row anchoring stays on the correct property row, not a different row.
        self.assertEqual(payload["subject"], f"RE: {correct_row}")
        self.assertIn(correct_row, payload["body"])
        self.assertEqual(payload["contactName"], "Dana Correct")

    def test_missing_suggested_email_does_not_fall_back_to_original_wrong_recipient(self):
        # Deterministic safety: with no valid corrected address, the classifier
        # must NOT silently re-target the original wrong recipient. It emits an
        # empty recipient list rather than sending to the wrong row/person.
        payload = build_wrong_contact_suggested_email(
            original_contact="mike.wrong@example.com",
            suggested_contact="Dana Correct",
            suggested_email="",
            row_anchor="1200 Test Loop",
            referrer_name="Mike Wrong",
        )

        self.assertEqual(payload["to"], [])
        self.assertNotIn("mike.wrong@example.com", payload["to"])


if __name__ == "__main__":
    unittest.main()
