"""End-to-end proof that the hook sits on the real send-path choke point."""
import os
import unittest
from unittest import mock

from email_automation.utils import (
    build_professional_signature_html,
    get_email_footer,
    format_email_body_with_footer,
    resolve_signature_settings,
)
from email_automation.signature_website_validation import (
    SIGNATURE_WEBSITE_VALIDATION_ENV as FLAG,
)

BAD_PROFILE = {
    "email": "agent@acme-brokerage.co",
    "signatureMode": "professional",
    "professionalSignature": {
        "name": "A Broker",
        "title": "Senior Advisor",
        "phone": "555-0100",
        "email": "agent@acme-brokerage.co",
        "company": "Acme Brokerage",
        "website": "yourcompany.com",
    },
}
GOOD_PROFILE = {
    **BAD_PROFILE,
    "professionalSignature": {**BAD_PROFILE["professionalSignature"], "website": "acme-brokerage.co"},
}


def on():
    return mock.patch.dict(os.environ, {FLAG: "true"}, clear=False)


def off():
    return mock.patch.dict(os.environ, {FLAG: ""}, clear=False)


class HookTests(unittest.TestCase):
    def _footer(self, profile):
        sig, mode, email = resolve_signature_settings(profile)
        return get_email_footer(sig, mode, user_email=email)

    def test_baseline_bad_website_is_linked_today(self):
        with off():
            footer = self._footer(BAD_PROFILE)
        self.assertIn('href="https://yourcompany.com"', footer)

    def test_flag_on_delinks_the_placeholder_website(self):
        with on():
            footer = self._footer(BAD_PROFILE)
        self.assertNotIn("https://yourcompany.com", footer)
        self.assertIn("yourcompany.com", footer)          # text still shown
        self.assertIn("mailto:agent@acme-brokerage.co", footer)
        self.assertIn("Acme Brokerage", footer)
        self.assertIn("555-0100", footer)

    def test_flag_on_leaves_a_real_website_untouched(self):
        with off():
            baseline = self._footer(GOOD_PROFILE)
        with on():
            gated = self._footer(GOOD_PROFILE)
        self.assertEqual(baseline, gated)
        self.assertIn('href="https://acme-brokerage.co"', gated)

    def test_flag_off_is_byte_identical_for_every_mode(self):
        for profile in (BAD_PROFILE, GOOD_PROFILE):
            sig, mode, email = resolve_signature_settings(profile)
            with off():
                a = get_email_footer(sig, mode, user_email=email)
            with mock.patch.dict(os.environ, {}, clear=True):
                b = get_email_footer(sig, mode, user_email=email)
            self.assertEqual(a, b)

    def test_custom_html_signature_is_gated_too(self):
        custom = (
            'Jane Doe<br>Acme Brokerage<br>'
            '<a href="http://192.168.1.50/team">our site</a>'
        )
        with on():
            footer = get_email_footer(custom, "custom", user_email="jane@acme-brokerage.co")
        self.assertNotIn("192.168.1.50", footer)
        self.assertIn("our site", footer)

    def test_full_body_assembly_also_gated(self):
        sig, mode, email = resolve_signature_settings(BAD_PROFILE)
        with on():
            html = format_email_body_with_footer("Hi, quick question.", sig, mode, user_email=email)
        self.assertNotIn("https://yourcompany.com", html)
        self.assertIn("Hi, quick question.", html)

    def test_validator_import_failure_fails_open(self):
        import email_automation.utils as utils
        sig, mode, email = resolve_signature_settings(BAD_PROFILE)
        with off():
            baseline = get_email_footer(sig, mode, user_email=email)
        with on():
            with mock.patch.dict("sys.modules", {"email_automation.signature_website_validation": None}):
                degraded = get_email_footer(sig, mode, user_email=email)
        self.assertEqual(baseline, degraded)


class MotivatingRealWorldCaseTests(unittest.TestCase):
    """The case that actually caused this work.

    A live test sent two structurally identical signatures. The one whose
    website pointed at a RESERVED placeholder domain landed in spam; the one
    pointing at a normal company domain landed in the inbox. So the two
    assertions that matter are a matched pair:

      * the reserved/placeholder link IS caught and neutralised, and
      * an ordinary real-world company domain is NOT caught -- because a false
        positive here silently deletes a paying customer's own website link.
    """

    def _profile(self, website):
        return {
            "email": "agent@northbridge-commercial.com",
            "signatureMode": "professional",
            "professionalSignature": {
                "name": "A Broker",
                "title": "Principal",
                "phone": "555-0100",
                "email": "agent@northbridge-commercial.com",
                "company": "Northbridge Commercial",
                "website": website,
            },
        }

    def _footer(self, website):
        profile = self._profile(website)
        sig, mode, email = resolve_signature_settings(profile)
        with on():
            return get_email_footer(sig, mode, user_email=email)

    def test_reserved_placeholder_domain_is_caught_and_delinked(self):
        # RFC 2606 reserved-for-documentation domain -- can never be a real site.
        footer = self._footer("example.com")
        self.assertNotIn('href="https://example.com"', footer)
        self.assertIn("example.com", footer)  # visible text survives
        self.assertIn("Northbridge Commercial", footer)
        self.assertIn("555-0100", footer)

    def test_ordinary_company_domain_is_not_caught(self):
        # The false-positive guard. An unremarkable real-world company domain
        # must come through with its link fully intact.
        for website in (
            "northbridge-commercial.com",
            "www.northbridge-commercial.com",
            "northbridge.realty",
            "northbridge-commercial.co.uk",
            "sample-of-the-day-media.com",
        ):
            with self.subTest(website=website):
                footer = self._footer(website)
                normalised = website if website.startswith("http") else "https://" + website
                self.assertIn(f'href="{normalised}"', footer)

    def test_the_pair_differs_only_in_the_link_target(self):
        """Same signature HTML shape either way -- only the href changes."""
        bad = self._footer("example.com")
        good = self._footer("northbridge-commercial.com")
        self.assertIn('target="_blank"', good)
        self.assertNotIn('target="_blank"', bad)
        self.assertIn("Principal", bad)
        self.assertIn("Principal", good)


if __name__ == "__main__":
    unittest.main()
