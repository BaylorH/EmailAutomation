import os

os.environ.setdefault("E2E_TEST_MODE", "true")

import unittest

# Exercise the REAL production signature resolver / builder.
from email_automation.utils import (
    resolve_signature_settings,
    format_email_body_with_footer,
)


# A cached signature that belongs to the MOHR/Jill identity. This is the kind of
# stale emailSignature HTML that can be left on an unrelated user's profile and
# must never be re-emitted for a sender who is not Jill.
JILL_MOHR_CACHED_SIGNATURE = (
    '<div data-sitesift-professional-signature="v1">'
    '<strong>Jill Ames</strong><br>'
    '<a href="mailto:jill.ames@mohrpartners.com">jill.ames@mohrpartners.com</a>'
    '<br>Mohr Partners, Inc.'
    '</div>'
)

# The unrelated sender who is composing this email.
WRONG_RECIPIENT_EMAIL = "casey.broker@example.com"


class CoreSignatureIdentityWrongRecipientTest(unittest.TestCase):
    """rubric: feature=core.signature_identity, class=wrong_recipient.

    Proves the real signature builder does not leak Jill/MOHR's identity
    signature onto an unrelated sender's outbound mail.
    """

    def test_cached_jill_mohr_signature_is_not_leaked_to_unrelated_sender(self):
        # Real function under test: resolve_signature_settings decides which
        # signature (if any) an outbound sender gets. The wrong-recipient guard
        # must reject a cached identity signature that belongs to someone else.
        signature, mode, resolved_email = resolve_signature_settings({
            "email": WRONG_RECIPIENT_EMAIL,
            "signatureMode": "custom",
            "emailSignature": JILL_MOHR_CACHED_SIGNATURE,
        })

        # The mode/sender are preserved, but the leaked identity signature is dropped.
        self.assertEqual("custom", mode)
        self.assertEqual(WRONG_RECIPIENT_EMAIL, resolved_email)
        self.assertIsNone(
            signature,
            "Jill/MOHR cached signature must not be returned for an unrelated sender",
        )

        # End-to-end: rendering the real outbound body for the wrong recipient
        # must contain neither Jill's name/email nor the MOHR org identity.
        html = format_email_body_with_footer(
            "Hi Avery,\n\nCould you confirm the rate for the tour?",
            signature,
            mode,
            user_email=resolved_email,
        )
        self.assertIn("Hi Avery", html)
        self.assertNotIn("Jill Ames", html)
        self.assertNotIn("jill.ames@mohrpartners.com", html)
        self.assertNotIn("Mohr Partners, Inc.", html)


if __name__ == "__main__":
    unittest.main()
