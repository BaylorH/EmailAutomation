"""Deterministic out-of-office guard — found via LIVE testing on real code.

Two real broker auto-replies classified NONDETERMINISTICALLY as wrong_contact:

  E1: "I'm out of office until July 10 with limited email access. For urgent
       matters, contact baylor@manifoldengineering.ai"
  E3: "OOO: traveling this week and slow to respond. Please contact my
       assistant baylor@manifoldengineering.ai for anything ..."

On some runs the LLM read the OOO backup / assistant address as a wrong_contact
redirect and escalated the WRONG person (events=['wrong_contact'],
escalated=True, suggestedEmail=[backup]). An auto-reply is not a human handoff.
This guard deterministically strips wrong_contact when the latest inbound is an
out-of-office / auto-reply, so behavior is model-independent.
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

from email_automation.ai_processing import (  # noqa: E402
    _augment_events_with_deterministic_signals,
    _detect_colleague_redirect,
    _looks_like_out_of_office,
)

BROKER = "sam.broker@example-cre.com"
BACKUP = "baylor@manifoldengineering.ai"

E1_BODY = (
    "I'm out of office until July 10 with limited email access. "
    "For urgent matters, contact " + BACKUP + "."
)
E3_BODY = (
    "OOO: traveling this week and slow to respond. Please contact my assistant "
    + BACKUP + " for anything time-sensitive."
)


def _conv(body):
    return [{"direction": "inbound", "from": BROKER, "to": ["baylor.freelance@outlook.com"],
             "subject": "Automatic reply: Space availability",
             "timestamp": "2026-07-05T03:55:00Z", "content": body}]


class DetectOutOfOfficeTests(unittest.TestCase):
    def test_e1_out_of_office_phrase(self):
        self.assertTrue(_looks_like_out_of_office(E1_BODY))

    def test_e3_ooo_token(self):
        self.assertTrue(_looks_like_out_of_office(E3_BODY))

    def test_genuine_wrong_contact_is_not_ooo(self):
        # A real handoff must NOT be swallowed by the OOO guard.
        body = ("I don't handle that property anymore, please reach out to "
                "Sarah Jones at sarah@broker.com going forward.")
        self.assertFalse(_looks_like_out_of_office(body))

    def test_colleague_redirect_is_not_ooo(self):
        body = ("It's available - 32,000 SF, $9.25 NNN. Also my colleague Dana "
                "Reyes (dana@example-cre.com) actually handles the south submarket, "
                "loop her in.")
        self.assertFalse(_looks_like_out_of_office(body))

    def test_plain_reply_is_not_ooo(self):
        body = "Hi John, it's available - 32,000 SF, $9.25 NNN. Flyer attached. Thanks, Sam"
        self.assertFalse(_looks_like_out_of_office(body))

    def test_limited_access_property_description_is_not_ooo(self):
        # "limited access" as a PROPERTY description must not read as OOO — else it
        # strips a genuine wrong_contact riding in the same reply.
        body = ("The site has limited access after hours. I no longer cover this "
                "one — please reach out to Dana at dana@example-cre.com.")
        self.assertFalse(_looks_like_out_of_office(body))

    def test_back_in_office_human_handoff_is_not_ooo(self):
        # A live human handoff that merely mentions "back in the office" is a real
        # wrong_contact, not an auto-reply banner.
        body = ("I was traveling, back in the office Monday. In the meantime please "
                "contact Dana at dana@example-cre.com for 900 Escalation Ct.")
        self.assertFalse(_looks_like_out_of_office(body))

    def test_back_in_office_human_handoff_preserves_wrong_contact(self):
        # End-to-end: the redirect must survive (not be stripped by the OOO guard).
        body = ("I was traveling, back in the office Monday. In the meantime please "
                "contact my colleague Dana Reyes (dana@example-cre.com) for that one.")
        proposal = {"events": [], "response_email": "Hi Sam, will do.", "updates": []}
        out = _augment_events_with_deterministic_signals(proposal, _conv(body))
        self.assertIn("wrong_contact", [e.get("type") for e in out["events"]])
        self.assertIsNone(out["response_email"])


class OutOfOfficeStripsWrongContactTests(unittest.TestCase):
    def _bad_llm_proposal(self):
        # Simulate the model's "bad" run: it fired wrong_contact and surfaced the
        # OOO backup address as the correct contact, nulling the reply (escalation).
        return {
            "events": [{
                "type": "wrong_contact",
                "reason": "forwarded",
                "suggestedEmail": BACKUP,
            }],
            "response_email": None,
            "updates": [],
        }

    def test_e1_strips_wrong_contact(self):
        out = _augment_events_with_deterministic_signals(self._bad_llm_proposal(), _conv(E1_BODY))
        types = [e.get("type") for e in out["events"]]
        self.assertNotIn("wrong_contact", types,
                         "OOO auto-reply backup contact must not escalate as wrong_contact")

    def test_e3_strips_wrong_contact(self):
        out = _augment_events_with_deterministic_signals(self._bad_llm_proposal(), _conv(E3_BODY))
        types = [e.get("type") for e in out["events"]]
        self.assertNotIn("wrong_contact", types,
                         "OOO 'contact my assistant <email>' must not escalate as wrong_contact")

    def test_ooo_does_not_force_redirect(self):
        # The redirect guard must not fire on an OOO backup contact either.
        empty = {"events": [], "response_email": None, "updates": []}
        out = _augment_events_with_deterministic_signals(empty, _conv(E3_BODY))
        self.assertNotIn("wrong_contact", [e.get("type") for e in out["events"]])

    def test_genuine_colleague_redirect_still_escalates(self):
        # Guard must NOT weaken the existing colleague-redirect escalation.
        body = ("It's available - 32,000 SF, $9.25 NNN. Also my colleague Dana Reyes "
                "(dana@example-cre.com) actually handles the south submarket, loop her in.")
        proposal = {"events": [], "response_email": "Hi Sam, I'll loop her in.", "updates": []}
        out = _augment_events_with_deterministic_signals(proposal, _conv(body))
        self.assertIn("wrong_contact", [e.get("type") for e in out["events"]])
        self.assertIsNone(out["response_email"])


if __name__ == "__main__":
    unittest.main()
