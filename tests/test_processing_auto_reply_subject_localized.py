"""Regression test for FIX-18 / M08 variant.

The RFC-3834 / subject auto-reply guard in `process_inbox_message` previously
matched only English/German/French auto-reply subjects. Localized auto-replies
(Spanish "Respuesta automática", Italian "Risposta automatica", Portuguese
"Resposta automática", Dutch "Automatisch antwoord", plus the accented French
form) bypassed the guard and reached the classifier with identical OOO
semantics, stalling the follow-up loop (see M08 analysis: the header guard is
the deterministic backstop for the temporary-absence misclassification).

The guard's subject-matching is exercised here as a pure function
(`_is_auto_reply_subject`) so the regression is deterministic and needs no live
Graph/model call.
"""

import unittest

from email_automation.processing import _is_auto_reply_subject


class AutoReplySubjectLocalizedTest(unittest.TestCase):
    def test_existing_subjects_still_recognized(self):
        for subject in [
            "Out of Office",
            "Automatic reply: Re: 1200 Industrial Blvd",
            "Auto-Reply",
            "Automatische Antwort",
            "Réponse automatique",
        ]:
            with self.subTest(subject=subject):
                self.assertTrue(_is_auto_reply_subject(subject))

    def test_localized_auto_reply_subjects_recognized(self):
        for subject in [
            "Respuesta automática",  # Spanish
            "Respuesta automatica: Re: propuesta",  # Spanish, unaccented
            "Risposta automatica",  # Italian
            "Resposta automática",  # Portuguese
            "Automatisch antwoord",  # Dutch
            "Ausencia temporal de la oficina",  # Spanish OOO phrase
        ]:
            with self.subTest(subject=subject):
                self.assertTrue(
                    _is_auto_reply_subject(subject),
                    f"localized auto-reply subject not recognized: {subject!r}",
                )

    def test_normal_broker_subject_is_not_flagged(self):
        for subject in [
            "Re: 1200 Industrial Blvd — availability",
            "Updated flyer attached",
            "Following up on your tour request",
        ]:
            with self.subTest(subject=subject):
                self.assertFalse(_is_auto_reply_subject(subject))


if __name__ == "__main__":
    unittest.main()
