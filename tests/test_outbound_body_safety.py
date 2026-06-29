import unittest

from email_automation import outbound_safety


class OutboundBodySafetyTests(unittest.TestCase):
    def test_name_placeholder_blocks_outbound_body(self):
        result = outbound_safety.validate_outbound_body(
            "Hi [NAME],\n\nCould you confirm the SF available?"
        )

        self.assertFalse(result.is_safe)
        self.assertIn("[NAME]", result.placeholders)

    def test_real_broker_name_passes_outbound_body(self):
        result = outbound_safety.validate_outbound_body(
            "Hi Connor,\n\nCould you confirm the SF available?"
        )

        self.assertTrue(result.is_safe)
        self.assertEqual([], result.placeholders)

    def test_tour_scheduling_language_blocks_normal_outreach(self):
        result = outbound_safety.validate_outbound_body(
            "Hi Connor,\n\nBefore we proceed with tour scheduling and/or LOIs, "
            "can you please confirm the following?"
        )

        self.assertFalse(result.is_safe)
        self.assertIn("tour", result.reason.lower())

    def test_reviewed_tour_invites_can_use_tour_language(self):
        result = outbound_safety.validate_outbound_body(
            "Hi Connor,\n\nWe are confirming the tour for Tuesday at 10:00 AM.",
            allow_scheduling_language=True,
        )

        self.assertTrue(result.is_safe)


if __name__ == "__main__":
    unittest.main()
