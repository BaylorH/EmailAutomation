"""Deterministic deferral-silence guard — found via LIVE testing on real code.

LIVE break (thread on 12870 W Indian School Rd, 2026-08-12): the broker said the
operating-expense figure was still coming, three times in a row. The system sent a
BYTE-IDENTICAL request for operating expenses three times in fourteen minutes --
03:43:09, 03:46:57 and 03:57:31 -- the third one twenty-five seconds after he wrote
"I will send operating expenses next".

The existing no-re-ask guard covers fields the system already HAS and verifiably
holds. This is the opposite case: a field the system is still MISSING, which the
broker has explicitly promised. A person waits. The follow-up scheduler already
owns the later nudge, so silence here loses nothing.

The guard is deliberately narrow. It fires only when all three hold:
  1. the broker's FRESH message states a deferral,
  2. that message asks us nothing (no question directed back at us), and
  3. the drafted reply is only a request for information.
Anything else -- a question to answer, a substantive reply, a first ask -- is
untouched, so a legitimate reply can never be swallowed by this guard.
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
from email_automation.ai_processing import (  # noqa: E402
    _augment_events_with_deterministic_signals,
    _looks_like_field_deferral,
)

BROKER = "bp21harrison+baymeadows-row07@gmail.com"

# The three FRESH broker messages that each drew an identical re-ask, verbatim
# from the live thread.
LIVE_DEFERRALS = [
    "Correction: the total area is 40,800 SF. Operating expenses are still pending.",
    "Correction: asking rent is $15.10/SF/year. Operating expenses are still pending.",
    "Please use 40,800 SF and $15.10/SF/year. I will send operating expenses next.",
]

# The exact authored body the system sent three times.
REPEATED_REASK = (
    "Hi Morgan,\n\nThank you for the information!\n\nTo complete the property "
    "details, could you please provide:\n\n- Ops Ex / SF"
)


def _conv(body):
    return [{"direction": "inbound", "from": BROKER, "to": ["baylor.freelance@outlook.com"],
             "subject": "Re: Inquiry - 9250 Baymeadows Rd", "timestamp": "2026-08-12T03:42:39Z",
             "content": body}]


def _proposal(response_email=REPEATED_REASK, updates=None, events=None):
    return {"updates": list(updates or []), "events": list(events or []),
            "response_email": response_email}


class DeferralDetectionTests(unittest.TestCase):
    def test_live_deferral_phrasings_all_detected(self):
        for body in LIVE_DEFERRALS:
            with self.subTest(body=body[:48]):
                self.assertTrue(_looks_like_field_deferral(body))

    def test_deferral_phrase_family_variants_all_fire(self):
        # The guard must not be tuned to the two phrasings that happened to appear
        # live, or the next wording silently repeats the same failure.
        for body in [
            "Ops ex is still outstanding, I'll send it over once I have it.",
            "I don't have those yet - waiting on the landlord.",
            "I will forward the CAM numbers next.",
            "Still waiting for the operating expense breakdown.",
            "That figure is coming shortly.",
        ]:
            with self.subTest(body=body[:48]):
                self.assertTrue(_looks_like_field_deferral(body))

    def test_plain_answers_are_not_deferrals(self):
        for body in [
            "The space is 41,200 SF.",
            "Operating expenses are $3.75/SF/year.",
            "$15.40/SF/year. Let's hop on a quick call before we continue.",
        ]:
            with self.subTest(body=body[:48]):
                self.assertFalse(_looks_like_field_deferral(body))


class DeferralSilenceGuardTests(unittest.TestCase):
    def test_repeated_reask_is_suppressed_on_every_live_deferral(self):
        """The live break: identical re-ask after an explicit deferral."""
        for body in LIVE_DEFERRALS:
            with self.subTest(body=body[:48]):
                out = _augment_events_with_deterministic_signals(_proposal(), _conv(body))
                self.assertIsNone(
                    out.get("response_email"),
                    "a stated deferral must be answered with silence, not the same request again",
                )

    def test_facts_in_the_same_message_are_still_written(self):
        """Silence is about the REPLY. The corrections he sent must still land."""
        updates = [{"field": "Total SF", "value": "40800"}]
        out = _augment_events_with_deterministic_signals(
            _proposal(updates=updates), _conv(LIVE_DEFERRALS[0])
        )
        self.assertEqual(out.get("updates"), updates)

    def test_question_from_the_broker_is_always_answered(self):
        """A deferral that also asks us something is a reply we owe him."""
        out = _augment_events_with_deterministic_signals(
            _proposal(), _conv("Ops ex is still pending. What is your client's timeline?")
        )
        self.assertIsNotNone(out.get("response_email"))

    def test_first_ask_is_untouched(self):
        """No deferral means the ordinary missing-field request still goes out."""
        out = _augment_events_with_deterministic_signals(
            _proposal(), _conv("The space is 41,200 SF.")
        )
        self.assertEqual(out.get("response_email"), REPEATED_REASK)

    def test_substantive_reply_survives_a_deferral(self):
        """Only a bare re-ask is suppressed; real content is never swallowed."""
        substantive = (
            "Hi Morgan,\n\nThat works - my client is targeting a Q1 occupancy and "
            "the 40,800 SF figure fits their footprint. I'll hold the file open."
        )
        out = _augment_events_with_deterministic_signals(
            _proposal(response_email=substantive), _conv(LIVE_DEFERRALS[2])
        )
        self.assertEqual(out.get("response_email"), substantive)

    def test_call_request_escalation_still_wins(self):
        """The guard must not disturb the call-request escalation."""
        out = _augment_events_with_deterministic_signals(
            _proposal(), _conv("Ops ex is still pending. Let's hop on a quick call.")
        )
        self.assertIsNone(out.get("response_email"))
        self.assertTrue(
            any((e or {}).get("type") == "call_requested" for e in out.get("events") or [])
        )


class MissingFieldsSendLaneTests(unittest.TestCase):
    """The re-ask that actually went out three times is the DETERMINISTIC template.

    Scenario 3 in ``process_inbox_message`` composes it from the missing-field list
    and ignores whatever the model drafted, which is exactly why the three live
    messages were byte-identical rather than merely similar. Suppressing the model's
    draft therefore cannot fix this on its own -- the send decision itself has to
    hold when the broker has just promised the value.
    """

    # The message as it arrives at the send lane: fresh text plus the quoted history
    # the broker's client appended underneath.
    QUOTED_TAIL = (
        "\n\nOn Tue, Aug 11, 2026 at 8:40 PM Baylor Harrison "
        "<baylor.freelance@outlook.com> wrote:\n"
        "> Understood. What are the operating expenses per square foot?\n"
        "> Best,\n> John Doe\n"
    )

    def test_every_live_deferral_holds_the_reply(self):
        for body in LIVE_DEFERRALS:
            with self.subTest(body=body[:48]):
                self.assertTrue(
                    processing._deferral_holds_missing_fields_reply(body + self.QUOTED_TAIL)
                )

    def test_quoted_history_alone_never_holds_a_reply(self):
        """A deferral that only appears in the QUOTED tail must not silence us."""
        fresh = "Here is the flyer with the rest of the details."
        self.assertFalse(
            processing._deferral_holds_missing_fields_reply(fresh + self.QUOTED_TAIL
                                                            + "> Ops ex is still pending.\n")
        )

    def test_plain_partial_answer_still_gets_the_request(self):
        self.assertFalse(
            processing._deferral_holds_missing_fields_reply(
                "The space is 41,200 SF." + self.QUOTED_TAIL
            )
        )

    def test_deferral_with_a_question_still_gets_a_reply(self):
        self.assertFalse(
            processing._deferral_holds_missing_fields_reply(
                "Ops ex is still pending. What is your client's timeline?" + self.QUOTED_TAIL
            )
        )


if __name__ == "__main__":
    unittest.main()
