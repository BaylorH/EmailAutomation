"""Regression test for the auto-reply subject substring OVER-match (CodeRabbit).

`_is_auto_reply_subject` previously matched every marker as a bare subject
substring. Two markers collide with legitimate CRE broker replies:

  * "fuori sede"  — Italian "off-site", frequently "off-site but AVAILABLE"
  * "on vacation" — e.g. "our tenant is on vacation until August, but the
                     space is available"

A human broker reply whose subject merely CONTAINS one of these was
misclassified as an auto-reply and dropped, stalling the follow-up loop.

The fix makes detection context-aware: unambiguous auto-responder strings still
match on the subject alone, but the two ambiguous phrases only count when an
independent auto-reply signal (RFC-3834 header or auto-responder sender)
corroborates them. This suite covers BOTH directions — the false positives must
no longer be dropped, and genuine auto-replies must still be caught.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.processing import (  # noqa: E402
    _is_auto_reply_sender,
    _is_auto_reply_subject,
)


class AutoReplySubjectOvermatchTest(unittest.TestCase):
    # --- Direction 1: false positives must NOT be flagged as auto-reply ------

    def test_human_broker_reply_with_ambiguous_phrase_not_flagged(self):
        """No corroborating signal -> ambiguous phrases are NOT auto-replies."""
        for subject in [
            "Re: 1200 Industrial Blvd — tenant on vacation but space available",
            "Fuori sede fino a lunedì, ma l'immobile è disponibile",  # off-site but available
            "Following up: our contact is on vacation, I can help meanwhile",
        ]:
            with self.subTest(subject=subject):
                self.assertFalse(
                    _is_auto_reply_subject(subject),
                    f"human broker reply wrongly flagged as auto-reply: {subject!r}",
                )
                # Explicit no-signal call must also stay False.
                self.assertFalse(
                    _is_auto_reply_subject(subject, has_auto_reply_signal=False),
                    f"human broker reply wrongly flagged (explicit no-signal): {subject!r}",
                )

    def test_human_broker_sender_is_not_an_auto_reply_sender(self):
        for sender in [
            "marco.rossi@brokerfirm.it",
            "jane.doe@cbre.com",
            "",
            "not-an-email",
        ]:
            with self.subTest(sender=sender):
                self.assertFalse(_is_auto_reply_sender(sender))

    # --- Direction 2: genuine auto-replies MUST still be caught --------------

    def test_unambiguous_markers_still_caught_on_subject_alone(self):
        for subject in [
            "Out of Office",
            "Automatic reply: Re: 1200 Industrial Blvd",
            "Auto-Reply",
            "Automatische Antwort",
            "Réponse automatique",
            "Risposta automatica",  # Italian auto-reply (unambiguous form retained)
            "Respuesta automática",
        ]:
            with self.subTest(subject=subject):
                self.assertTrue(
                    _is_auto_reply_subject(subject),
                    f"genuine auto-reply subject not caught: {subject!r}",
                )

    def test_ambiguous_phrase_caught_when_signal_present(self):
        """With a corroborating signal, the ambiguous phrases ARE auto-replies."""
        for subject in [
            "Sono fuori sede",  # true Italian OOO
            "I'm on vacation until August",
        ]:
            with self.subTest(subject=subject):
                self.assertTrue(
                    _is_auto_reply_subject(subject, has_auto_reply_signal=True),
                    f"corroborated auto-reply not caught: {subject!r}",
                )

    def test_auto_responder_sender_is_recognized(self):
        for sender in [
            "no-reply@brokerfirm.it",
            "noreply@cbre.com",
            "MAILER-DAEMON@mx.example.com",
            "postmaster@example.com",
            "donotreply@notifications.example.com",
        ]:
            with self.subTest(sender=sender):
                self.assertTrue(_is_auto_reply_sender(sender))

    def test_end_to_end_ambiguous_subject_from_autoresponder_sender(self):
        """Ambiguous subject + auto-responder sender -> still classified auto-reply."""
        sender = "no-reply@brokerfirm.it"
        subject = "Fuori sede"
        signal = _is_auto_reply_sender(sender)
        self.assertTrue(signal)
        self.assertTrue(
            _is_auto_reply_subject(subject, has_auto_reply_signal=signal)
        )

    def test_end_to_end_ambiguous_subject_from_human_sender_not_dropped(self):
        """Ambiguous subject + real broker sender -> NOT classified auto-reply."""
        sender = "marco.rossi@brokerfirm.it"
        subject = "Fuori sede ma disponibile per una visita"
        signal = _is_auto_reply_sender(sender)
        self.assertFalse(signal)
        self.assertFalse(
            _is_auto_reply_subject(subject, has_auto_reply_signal=signal)
        )


if __name__ == "__main__":
    unittest.main()
