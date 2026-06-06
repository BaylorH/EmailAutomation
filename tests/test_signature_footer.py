import unittest

from email_automation.utils import (
    format_email_body_with_footer,
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

    def test_professional_mode_without_user_signature_does_not_use_jill_for_non_mohr_users(self):
        footer = get_email_footer(
            "",
            "professional",
            user_email="baylor.freelance@outlook.com",
        )

        self.assertEqual("", footer)
        self.assertFalse(
            needs_signature_attachments(
                "professional",
                "",
                user_email="baylor.freelance@outlook.com",
            )
        )

    def test_professional_mode_without_user_signature_keeps_legacy_mohr_for_mohr_users(self):
        footer = get_email_footer(
            "",
            "professional",
            user_email="jill.ames@mohrpartners.com",
        )

        self.assertIn("Jill Ames", footer)
        self.assertTrue(
            needs_signature_attachments(
                "professional",
                "",
                user_email="jill.ames@mohrpartners.com",
            )
        )

    def test_format_body_does_not_append_jill_for_empty_non_mohr_professional_signature(self):
        html = format_email_body_with_footer(
            "Hi BP21,\n\nTest body.",
            "",
            "professional",
            user_email="baylor.freelance@outlook.com",
        )

        self.assertIn("Hi BP21", html)
        self.assertNotIn("Jill Ames", html)
        self.assertNotIn("jill.ames@mohrpartners.com", html)


if __name__ == "__main__":
    unittest.main()
