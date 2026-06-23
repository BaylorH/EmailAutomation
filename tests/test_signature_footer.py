import unittest
import base64
import io
import random

from PIL import Image

from email_automation.utils import (
    SIGNATURE_INLINE_IMAGE_MAX_BYTES,
    build_professional_signature_html,
    format_email_body_with_footer,
    get_signature_attachments,
    get_email_footer,
    resolve_signature_settings,
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

    def test_signature_attachments_do_not_default_to_company_logo_without_profile_context(self):
        self.assertEqual([], get_signature_attachments())

    def test_professional_mode_without_user_signature_does_not_use_legacy_jill(self):
        footer = get_email_footer(
            "",
            "professional",
            user_email="jill.ames@mohrpartners.com",
        )

        self.assertEqual("", footer)
        self.assertFalse(
            needs_signature_attachments(
                "professional",
                "",
                user_email="jill.ames@mohrpartners.com",
            )
        )

    def test_resolved_professional_signature_uses_structured_fields_over_stale_cached_html(self):
        stale_jill_html = (
            '<div data-sitesift-professional-signature="v1">'
            '<strong>Jill Ames</strong><a href="mailto:jill.ames@mohrpartners.com">jill.ames@mohrpartners.com</a>'
            '</div>'
        )

        signature, mode, user_email = resolve_signature_settings({
            "email": "baylor.freelance@outlook.com",
            "signatureMode": "professional",
            "emailSignature": stale_jill_html,
            "professionalSignature": {
                "name": "John Doe",
                "title": "Principal",
                "email": "baylor.freelance@outlook.com",
                "company": "Example Realty Advisors",
            },
        })

        self.assertEqual("professional", mode)
        self.assertEqual("baylor.freelance@outlook.com", user_email)
        self.assertIn("John Doe", signature)
        self.assertIn("Example Realty Advisors", signature)
        self.assertNotIn("Jill Ames", signature)
        self.assertNotIn("jill.ames@mohrpartners.com", signature)

    def test_mohr_domain_defaults_fill_company_branding_without_person_impersonation(self):
        signature, mode, user_email = resolve_signature_settings({
            "email": "drew.ingram@mohrpartners.com",
            "displayName": "Drew Ingram",
            "signatureMode": "professional",
            "professionalSignature": {
                "title": "Advisor",
            },
        })
        footer = get_email_footer(signature, mode, user_email=user_email)
        attachments = get_signature_attachments(signature, mode, user_email=user_email)

        self.assertIn("Drew Ingram", footer)
        self.assertIn("drew.ingram@mohrpartners.com", footer)
        self.assertIn("Mohr Partners, Inc.", footer)
        self.assertIn('src="cid:signature-custom-logo-1"', footer)
        self.assertNotIn("Jill Ames", footer)
        self.assertEqual(1, len(attachments))

    def test_professional_signature_uses_saved_first_name_when_display_name_missing(self):
        signature, mode, user_email = resolve_signature_settings({
            "email": "baylor.freelance@outlook.com",
            "firstName": "Baylor",
            "signatureMode": "professional",
            "professionalSignature": {
                "title": "Principal",
                "email": "baylor.freelance@outlook.com",
                "company": "Manifold Engineering",
            },
        })

        self.assertEqual("professional", mode)
        self.assertEqual("baylor.freelance@outlook.com", user_email)
        self.assertIn("Baylor", signature)
        self.assertNotIn("BP21", signature)
        self.assertNotIn("Jill Ames", signature)

    def test_professional_signature_without_logo_stays_presentable_and_attachment_free(self):
        signature = build_professional_signature_html({
            "name": "Avery Broker",
            "title": "Industrial Advisor",
            "email": "avery@example.com",
            "company": "Example Realty Advisors",
        })

        self.assertIn("Avery Broker", signature)
        self.assertIn("Example Realty Advisors", signature)
        self.assertNotIn("<img", signature)
        self.assertEqual([], get_signature_attachments(signature, "professional", user_email="avery@example.com"))

    def test_professional_signature_logo_scales_without_forcing_fixed_width(self):
        signature = build_professional_signature_html({
            "name": "Avery Broker",
            "email": "avery@example.com",
            "company": "Example Realty Advisors",
            "logoDataUrl": "data:image/png;base64,LOGO",
        })

        self.assertIn("max-width:120px;max-height:150px;width:auto;height:auto", signature)
        self.assertNotIn('style="width:120px;max-width:120px;max-height:150px', signature)

    def test_professional_mode_without_user_signature_does_not_use_jill_for_other_mohr_users(self):
        footer = get_email_footer(
            "",
            "professional",
            user_email="drew.ingram@mohrpartners.com",
        )

        self.assertEqual("", footer)
        self.assertFalse(
            needs_signature_attachments(
                "professional",
                "",
                user_email="drew.ingram@mohrpartners.com",
            )
        )
        self.assertEqual(
            [],
            get_signature_attachments(
                "",
                "professional",
                user_email="drew.ingram@mohrpartners.com",
            ),
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

    def test_format_body_normalizes_smart_punctuation_before_graph_send(self):
        html = format_email_body_with_footer(
            "Hi Casey,\n\nI\u2019ll send the tour packet\u2014please review \u201cSuite B\u201d \u2022 NNN.",
            "",
            "none",
        )

        self.assertIn("I'll send the tour packet-please review \"Suite B\" - NNN.", html)
        self.assertNotIn("\u2019", html)
        self.assertNotIn("\u2014", html)
        self.assertNotIn("\u201c", html)
        self.assertNotIn("\u201d", html)
        self.assertNotIn("\u2022", html)

    def test_format_body_strips_broker_signoff_before_user_signature(self):
        html = format_email_body_with_footer(
            "Hi Casey,\n\n2:15 PM is still available for 4402 Rex Rd if that works on your end.\n\nThanks,\nBP21",
            "John Doe\nExample Realty Advisors",
            "professional",
            user_email="baylor.freelance@outlook.com",
        )

        self.assertIn("2:15 PM is still available", html)
        self.assertIn("John Doe", html)
        self.assertIn("Example Realty Advisors", html)
        self.assertNotIn("BP21", html)

    def test_format_body_preserves_manual_signoff_without_configured_signature(self):
        html = format_email_body_with_footer(
            "Hi Casey,\n\n2:15 PM is still available.\n\nThanks,\nBP21",
            "",
            "none",
        )

        self.assertIn("Thanks,<br>BP21", html)

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

    def test_oversized_custom_logo_attachment_is_resized_before_send(self):
        width = height = 420
        rng = random.Random(42)
        image = Image.new("RGB", (width, height))
        image.putdata([
            (rng.randrange(256), rng.randrange(256), rng.randrange(256))
            for _ in range(width * height)
        ])

        raw = io.BytesIO()
        image.save(raw, format="PNG")
        original_bytes = raw.getvalue()
        self.assertGreater(len(original_bytes), SIGNATURE_INLINE_IMAGE_MAX_BYTES)

        logo_b64 = base64.b64encode(original_bytes).decode("ascii")
        signature = f"""<!-- sitesift:professional-signature:v1 -->
<div data-sitesift-professional-signature="v1">
Best,<br>
<table><tr><td><img src="data:image/png;base64,{logo_b64}" alt="Huge logo"></td><td><strong>Logo User</strong></td></tr></table>
</div>"""

        attachments = get_signature_attachments(signature, "professional", user_email="logo.user@example.com")

        self.assertEqual(1, len(attachments))
        resized_bytes = base64.b64decode(attachments[0]["contentBytes"])
        self.assertLessEqual(len(resized_bytes), SIGNATURE_INLINE_IMAGE_MAX_BYTES)
        resized = Image.open(io.BytesIO(resized_bytes))
        self.assertLessEqual(max(resized.size), 240)


if __name__ == "__main__":
    unittest.main()
