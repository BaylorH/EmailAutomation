import unittest
import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation.utils import _sanitize_url, is_valid_email, validate_recipient_emails


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


if __name__ == "__main__":
    unittest.main()
