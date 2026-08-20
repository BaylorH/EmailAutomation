"""The system must never send the same message twice — the universal backstop.

Two separate real-customer threads, two different root causes, one identical
symptom, and the same commercial damage both times:

  * One broker got the same operating-expense request three times in fourteen
    minutes and replied "Are you using Ai or something to email me. This is
    ridiculous. I have answered these questions."

  * Another got the identical "could you please provide: - Ops Ex / SF" FOUR
    times across three days, twice in reply to messages that carried no property
    data at all -- once to "Do we know Opex, Flavio?" (he was asking his own
    colleague) and once to "I have cc'd my marketing partner multiple times and
    you are replying to me only. Please reply all." The client's own broker had
    to step in and write "Sorry about that, I use email automation software.
    I have manually stopped it for this property."

Every specific cause is worth fixing on its own merits, and several have been.
But causes are open-ended and the symptom is not, so the symptom gets its own
guard: whatever the reason, the same words do not go out twice in a row.

Deliberately a LOOKBACK rather than a comparison with the single previous
outbound -- in the second thread an unrelated follow-up landed between two of
the identical requests, and a last-message-only check would have waved it through.
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

from email_automation import processing  # noqa: E402

SIG = ("\n\nBest,\nJill Ames\nSenior Associate National Accounts\n"
       "License Nos. 127384 (WA), SP24646 (ID)\nT +1 206 510 5575")

REASK = ("Hi Joel,\n\nThank you for the information!\n\nTo complete the property "
         "details, could you please provide:\n\n- Ops Ex / SF")

FOLLOWUP = ("Hi Joel,\n\nI wanted to follow up on my previous email regarding the "
            "property above.")


class RepeatDetectionTests(unittest.TestCase):
    def test_the_same_request_twice_in_a_row_is_a_repeat(self):
        self.assertTrue(processing._repeats_recent_outbound(REASK + SIG, [REASK + SIG]))

    def test_a_repeat_is_caught_across_an_intervening_follow_up(self):
        """The exact shape of the live break a last-message-only check would miss."""
        self.assertTrue(
            processing._repeats_recent_outbound(REASK + SIG, [REASK + SIG, FOLLOWUP + SIG])
        )

    def test_a_different_message_is_not_a_repeat(self):
        self.assertFalse(processing._repeats_recent_outbound(FOLLOWUP + SIG, [REASK + SIG]))

    def test_a_changed_signature_does_not_make_it_a_new_message(self):
        """Only the authored part counts; a footer edit is not new content."""
        self.assertTrue(
            processing._repeats_recent_outbound(
                REASK + "\n\nBest,\nJill Ames\nBoise, ID", [REASK + SIG]
            )
        )

    def test_reflowed_whitespace_is_still_a_repeat(self):
        reflowed = " ".join((REASK + SIG).split())
        self.assertTrue(processing._repeats_recent_outbound(reflowed, [REASK + SIG]))

    def test_html_and_plain_text_of_the_same_message_match(self):
        html_body = ("<html><body><div>Hi Joel,<br><br>Thank you for the information!"
                     "<br><br>To complete the property details, could you please "
                     "provide:<br><br>- Ops Ex / SF<br><br>Best,<br>Jill Ames</div>"
                     "</body></html>")
        self.assertTrue(processing._repeats_recent_outbound(REASK + SIG, [html_body]))

    def test_no_history_is_never_a_repeat(self):
        self.assertFalse(processing._repeats_recent_outbound(REASK + SIG, []))

    def test_an_empty_draft_is_not_treated_as_a_repeat(self):
        self.assertFalse(processing._repeats_recent_outbound("", [REASK + SIG]))

    def test_an_old_message_beyond_the_lookback_is_allowed_again(self):
        """A legitimate re-ask much later in a long thread is not blocked forever."""
        history = [REASK + SIG] + [f"Message {i}" + SIG for i in range(5)]
        self.assertFalse(processing._repeats_recent_outbound(REASK + SIG, history))

    def test_quoted_history_is_ignored_when_comparing(self):
        with_quote = REASK + SIG + "\n\nOn Mon, Jun 8, 2026 at 9:00 AM Joel wrote:\n> anything"
        self.assertTrue(processing._repeats_recent_outbound(with_quote, [REASK + SIG]))


if __name__ == "__main__":
    unittest.main()
