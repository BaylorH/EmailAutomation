import unittest
import base64

from email_automation.utils import (
    format_email_body_with_footer,
    get_signature_attachments,
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

    def test_professional_html_signature_uses_custom_inline_logo_attachment(self):
        logo_bytes = base64.b64encode(b"fake-logo").decode("ascii")
        signature = f"""<!-- sitesift:professional-signature:v1 -->
<div data-sitesift-professional-signature="v1">
Best,<br>
<table><tr><td><img src="data:image/png;base64,{logo_bytes}" alt="Example Realty logo"></td><td><strong>Drew Ingram</strong></td></tr></table>
</div>"""

        footer = get_email_footer(
            signature,
            "professional",
            user_email="drew.ingram@mohrpartners.com",
        )
        attachments = get_signature_attachments(
            signature,
            "professional",
            user_email="drew.ingram@mohrpartners.com",
        )

        self.assertIn('data-sitesift-professional-signature="v1"', footer)
        self.assertIn("Drew Ingram", footer)
        self.assertIn('src="cid:signature-custom-logo-1"', footer)
        self.assertNotIn("data:image/png;base64", footer)
        self.assertEqual(1, len(attachments))
        self.assertEqual("signature-custom-logo-1", attachments[0]["contentId"])
        self.assertEqual("image/png", attachments[0]["contentType"])
        self.assertEqual(logo_bytes, attachments[0]["contentBytes"])
        self.assertTrue(
            needs_signature_attachments(
                "professional",
                signature,
                user_email="drew.ingram@mohrpartners.com",
            )
        )

    def test_professional_html_signature_does_not_fall_back_to_jill_for_mohr_user(self):
        signature = '<div data-sitesift-professional-signature="v1"><strong>Jill Ames Custom</strong></div>'

        footer = get_email_footer(
            signature,
            "professional",
            user_email="jill.ames@mohrpartners.com",
        )

        self.assertIn("Jill Ames Custom", footer)
        self.assertNotIn("License Nos. 127384", footer)


if __name__ == "__main__":
    unittest.main()
