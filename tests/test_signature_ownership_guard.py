"""Signature-ownership fail-closed guard — prevents cross-account identity leaks.

Root cause of the live incident: a stale build sent Jill Ames' MOHR signature on a
send from another account. The generic fix: a signature is only sent AS the sender
if it names the sender or carries no other person's email; the final send-path gate
(get_email_footer) drops any foreign-identity signature rather than leaking it.
"""
import os
import sys
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from email_automation.utils import (  # noqa: E402
    _professional_signature_html_belongs_to_sender,
    get_email_footer,
)

JOHN = "<div>John Doe<br>Principal<br>Example Realty Advisors<br>john@example.com</div>"
JILL = "<div>Jill Ames<br>Senior Associate<br>jill.ames@mohrpartners.com</div>"
TYNEESIA = "<div>Tyneesia Rogers<br>tyneesia.rogers@mohrpartners.com</div>"
NO_EMAIL = "<div>John Doe<br>Principal<br>Example Realty Advisors</div>"


class BelongsToSenderTests(unittest.TestCase):
    def test_own_email_belongs(self):
        self.assertTrue(_professional_signature_html_belongs_to_sender(JOHN, "john@example.com"))

    def test_foreign_jill_rejected(self):
        self.assertFalse(_professional_signature_html_belongs_to_sender(JILL, "john@example.com"))

    def test_foreign_non_jill_also_rejected(self):
        # the generic guard blocks ANY other user, not just the historical Jill case
        self.assertFalse(_professional_signature_html_belongs_to_sender(TYNEESIA, "john@example.com"))

    def test_no_email_in_signature_is_allowed(self):
        self.assertTrue(_professional_signature_html_belongs_to_sender(NO_EMAIL, "john@example.com"))

    def test_empty_signature_rejected(self):
        self.assertFalse(_professional_signature_html_belongs_to_sender("", "john@example.com"))


class GetEmailFooterFailClosedTests(unittest.TestCase):
    def test_professional_drops_foreign_signature(self):
        out = get_email_footer(JILL, "professional", user_email="john@example.com")
        self.assertEqual(out, "", "a foreign-identity signature must be dropped, not sent")
        self.assertNotIn("Jill", out)
        self.assertNotIn("mohrpartners", out)

    def test_professional_keeps_own_signature(self):
        out = get_email_footer(JOHN, "professional", user_email="john@example.com")
        self.assertIn("John Doe", out)

    def test_jills_own_send_keeps_her_signature(self):
        # Jill sending as herself is legitimate.
        out = get_email_footer(JILL, "professional", user_email="jill.ames@mohrpartners.com")
        self.assertIn("Jill Ames", out)


if __name__ == "__main__":
    unittest.main()
