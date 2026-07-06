import unittest
import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation.utils import (
    _sanitize_url,
    is_valid_email,
    strip_email_quotes,
    strip_html_tags,
    validate_recipient_emails,
)


class EmailValidationTests(unittest.TestCase):
    def test_reserved_test_domains_are_not_valid_send_recipients(self):
        self.assertFalse(is_valid_email("casey.test@example.invalid"))
        self.assertFalse(is_valid_email("someone@example.test"))
        self.assertFalse(is_valid_email("someone@example.example"))
        self.assertFalse(is_valid_email("someone@localhost"))

    def test_validate_recipient_emails_separates_reserved_domains(self):
        valid, invalid = validate_recipient_emails([
            "bp21harrison@gmail.com",
            "casey.test@example.invalid",
        ])

        self.assertEqual(valid, ["bp21harrison@gmail.com"])
        self.assertEqual(invalid, ["casey.test@example.invalid"])

    def test_sanitize_url_removes_glued_email_signoff_after_document_link(self):
        dirty_url = "https://example.com/launch-proof/808-fresh-proof-flyer.pdfThanks,Morgan"

        self.assertEqual(
            _sanitize_url(dirty_url),
            "https://example.com/launch-proof/808-fresh-proof-flyer.pdf",
        )

    def test_sanitize_url_keeps_document_query_strings(self):
        signed_url = "https://example.com/flyer.pdf?token=ThanksMorgan&download=1"

        self.assertEqual(_sanitize_url(signed_url), signed_url)

    def test_strip_html_tags_preserves_reply_boundaries_for_quote_trimming(self):
        html = (
            "<div>Hi John,</div>"
            "<div>Here are the property details.</div>"
            "<div>Best,</div>"
            "<div>BP21 Broker</div>"
            "<div>On Wed, Jun 17, 2026 at 9:28 PM Baylor wrote:</div>"
            "<blockquote>Hi Ryan, can you send the specs?</blockquote>"
        )

        text = strip_html_tags(html)

        self.assertRegex(text, r"BP21 Broker\s+On Wed")
        self.assertNotIn("can you send the specs", strip_email_quotes(text))


if __name__ == "__main__":
    unittest.main()
