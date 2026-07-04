import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation import email as email_mod


class CoreReplyAllCcBadPlaceholderTests(unittest.TestCase):
    """Rubric: core.reply_all_cc / bad_placeholder.

    In the outbox reply-all send path (email.py ~2848) the real production gate
    ``_dead_letter_unsafe_outbound_body_if_needed`` runs BEFORE
    ``_send_outbox_as_reply`` is ever invoked. It delegates to
    ``outbound_safety.validate_outbound_body``. This test exercises that real
    gate against a reply-all body that still carries an unresolved ``[NAME]``
    placeholder and proves the reply is dead-lettered (blocked) instead of
    reaching the Graph reply-all send. Only the datastore boundary
    (``_move_to_dead_letter``) is patched; the placeholder detection and the
    guard decision run for real.
    """

    def test_unresolved_placeholder_blocks_reply_all_before_send(self):
        # A dashboard reply-all body that was never personalized: the [NAME]
        # token survived into what would become the reply-all draft body.
        bad_body = "Hi [NAME],\n\nThanks for looping in the team. Following up on the space."
        # Not a tour invite -> no scheduling-language allowance; plain reply.
        data = {"threadId": "thread-1", "replyToMessageId": "msg-root"}

        with patch.object(email_mod, "_move_to_dead_letter") as dead_letter:
            blocked = email_mod._dead_letter_unsafe_outbound_body_if_needed(
                "uid-1",
                object(),  # doc_ref: opaque; datastore boundary is patched out
                data,
                bad_body,
            )

        # The bad-placeholder reply-all body is blocked before any send occurs.
        self.assertTrue(blocked)
        dead_letter.assert_called_once()
        # Fail-closed with an actionable, placeholder-specific reason.
        reason = dead_letter.call_args.args[3]
        self.assertIn("[NAME]", reason)
        self.assertIn("manual review required", reason)

        # ---- Negative control (discriminating) ----
        # An identical reply-all path with the placeholder actually resolved must
        # NOT be blocked, proving the assertion keys on the unresolved
        # placeholder and not on some always-on behavior.
        good_body = "Hi Sarah,\n\nThanks for looping in the team. Following up on the space."
        with patch.object(email_mod, "_move_to_dead_letter") as dead_letter_ok:
            allowed = email_mod._dead_letter_unsafe_outbound_body_if_needed(
                "uid-1",
                object(),
                data,
                good_body,
            )

        self.assertFalse(allowed)
        dead_letter_ok.assert_not_called()


if __name__ == "__main__":
    unittest.main()
