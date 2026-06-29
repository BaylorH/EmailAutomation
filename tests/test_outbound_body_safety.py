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


if __name__ == "__main__":
    unittest.main()
