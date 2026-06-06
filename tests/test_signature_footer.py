import unittest

from email_automation.utils import (
    get_email_footer,
    needs_signature_attachments,
)


class SignatureFooterTests(unittest.TestCase):
    def test_professional_mode_uses_user_signature_when_present(self):
        footer = get_email_footer(
            "Baylor Harrison\nbaylor.freelance@outlook.com",
            "professional",
        )

        self.assertIn("Baylor Harrison", footer)
        self.assertIn("baylor.freelance@outlook.com", footer)
        self.assertNotIn("Jill Ames", footer)
        self.assertNotIn("jill.ames@mohrpartners.com", footer)

    def test_professional_mode_with_user_signature_does_not_need_mohr_attachments(self):
        self.assertFalse(
            needs_signature_attachments(
                "professional",
                "Baylor Harrison\nbaylor.freelance@outlook.com",
            )
        )

    def test_professional_mode_without_user_signature_keeps_mohr_attachments(self):
        self.assertTrue(needs_signature_attachments("professional", ""))


if __name__ == "__main__":
    unittest.main()
