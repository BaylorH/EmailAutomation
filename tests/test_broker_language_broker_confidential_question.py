"""Pressure test: broker_confidential_question safety.

EVENT: broker_confidential_question
A broker replies asking us to disclose confidential client/tenant identity or
protected deal terms ("Who is the client?", "Can you disclose the tenant name
before we share pricing?", "What is their credit and intended use?").

DETECTION is LLM-only: `ai_processing._augment_events_with_deterministic_signals`
does NOT emit or protect the `needs_user_input:confidential` event -- the model
decides that. There is no deterministic classifier for the confidential question
itself, so we cannot test detection deterministically.

The one DETERMINISTIC SAFETY guard on the wire is
`outbound_safety.validate_outbound_body`, called by
`processing.send_reply_in_thread` immediately before an auto-reply is sent
(processing.py ~2515). It is the last line of defense against the two stop
conditions for this event:
  - "system reveals confidential client identity"
  - "system fabricates approval or budget details"

This file drives that real guard on the kind of reply body an auto-responder
could emit in a confidential-question thread, plus asserts the deterministic
event-augment layer does not stomp a model-emitted confidential flag.

No Firestore / Sheets / Graph / OpenAI calls are made: we call the pure
functions directly.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.outbound_safety import validate_outbound_body
from email_automation.ai_processing import _augment_events_with_deterministic_signals


def _inbound(text):
    return [{"direction": "inbound", "content": text}]


# --- Real-threat outbound reply bodies -------------------------------------
# These are the kinds of replies an auto-responder could generate WHEN a broker
# asked a confidential question. If any of these ever reaches send_reply_in_thread,
# validate_outbound_body is the ONLY thing standing between it and the broker.
# Correct behavior: the guard must refuse to send (is_safe == False).
#
# Each maps a broker confidential-question phrasing -> a leaking reply.
LEAKING_REPLIES = [
    # terse identity disclosure
    ("Who is the client?", "Our client is Acme Logistics Inc."),
    # verbose disclosure
    ("Can you disclose the tenant name before we share pricing?",
     "Happy to help! The tenant we represent on this requirement is Northstar "
     "Robotics, and they are actively touring the market this quarter."),
    # credit + intended use disclosure
    ("What is their credit and intended use?",
     "Their credit is strong (S&P BBB+) and the intended use is last-mile "
     "distribution for Acme Logistics."),
    # typo'd broker question -> disclosure
    ("whos ur clietn??",
     "No problem - the client on this one is Zephyr Foods LLC."),
    # partial / one-word question -> disclosure
    ("Client?",
     "The client is Meridian Health Systems."),
    # ALL CAPS
    ("WHO IS THE END USER ON THIS DEAL?",
     "THE END USER IS DELTA MANUFACTURING CORP."),
    # regional phrasing
    ("Reckon you could tell us who the covenant is before we quote?",
     "The covenant behind the requirement is Britannia Retail Group plc."),
    # multi-intent: pricing + identity
    ("Send me the asking rent and let me know who your client is.",
     "Asking is $12.50/SF NNN and our client is Acme Logistics Inc."),
    # quoted-history reply
    ("On Tue you wrote: 'we rep a growing tenant'. Can you name them?",
     "Yes - the tenant is GrowFast Ecommerce, as referenced earlier."),
    # with a signature block after the leak
    ("Who are you representing?",
     "We represent Acme Logistics Inc. on this search.\n\n--\n"
     "Jane Broker | Tenant Advisory | (555) 010-1234"),
    # disclosure phrased as reassurance
    ("Before pricing, I need to know the tenant.",
     "For transparency, the tenant is Acme Logistics Inc.; let's keep moving."),
    # naming the company mid-sentence
    ("Can you confirm the user?",
     "Confirming the user for this requirement is Acme Logistics Inc."),
]

# Fabricated approval / budget disclosure (second stop condition).
FABRICATED_APPROVAL_REPLIES = [
    ("What's their budget?",
     "Our client has a fully approved budget of $2.4M for this deal."),
    ("Is the client approved to move forward?",
     "Yes, the client's board has already approved this lease and signed off "
     "on terms up to $15/SF."),
    ("What can they spend?",
     "They are pre-approved to spend up to $18/SF NNN and can close in 30 days."),
    ("Do they have financing?",
     "Financing is fully secured and approved by their lender for the full "
     "commitment."),
]


# --- Near-miss reply bodies (safe deferrals) -------------------------------
# A correct auto-responder reply defers without leaking. These MUST send
# (is_safe == True) -- blocking them would kill legitimate broker email.
SAFE_DEFERRAL_REPLIES = [
    "Thanks for reaching out. I'll need to check with my client before I can "
    "share that, and will follow up shortly.",
    "Appreciate the question. I'm not able to disclose the tenant's identity at "
    "this stage, but I'm happy to keep the conversation going.",
    "Good question - let me confirm what I can share on our side and revert.",
    "I can't speak to credit or intended use just yet; I'll come back to you "
    "once I've cleared it internally.",
    "Understood on pricing. Before we get there, could you confirm the available "
    "square footage and asking rate?",
]


class OutboundSafetyBlocksConfidentialDisclosure(unittest.TestCase):
    """validate_outbound_body is the last deterministic safety gate before an
    auto-reply is sent (processing.send_reply_in_thread). It must refuse any
    reply that discloses confidential client identity or fabricates approval /
    budget -- the two stop conditions for this event class."""

    def test_leaking_identity_replies_are_blocked(self):
        for broker_q, reply in LEAKING_REPLIES:
            with self.subTest(broker=broker_q):
                v = validate_outbound_body(reply)
                self.assertFalse(
                    v.is_safe,
                    msg=(
                        "FALSE NEGATIVE (safety hole): reply disclosing "
                        f"confidential client identity was passed as safe.\n"
                        f"  broker asked: {broker_q!r}\n"
                        f"  auto-reply:   {reply!r}\n"
                        "validate_outbound_body has no confidential-disclosure "
                        "check, so send_reply_in_thread would wire this to the "
                        "broker."
                    ),
                )

    def test_fabricated_approval_budget_replies_are_blocked(self):
        for broker_q, reply in FABRICATED_APPROVAL_REPLIES:
            with self.subTest(broker=broker_q):
                v = validate_outbound_body(reply)
                self.assertFalse(
                    v.is_safe,
                    msg=(
                        "FALSE NEGATIVE (safety hole): reply fabricating "
                        f"approval/budget was passed as safe.\n"
                        f"  broker asked: {broker_q!r}\n"
                        f"  auto-reply:   {reply!r}"
                    ),
                )

    def test_safe_deferral_replies_are_allowed(self):
        # Control: legitimate non-leaking replies must not be blocked.
        for reply in SAFE_DEFERRAL_REPLIES:
            with self.subTest(reply=reply[:40]):
                v = validate_outbound_body(reply)
                self.assertTrue(
                    v.is_safe,
                    msg=(
                        "FALSE POSITIVE: safe deferral reply was blocked "
                        f"({v.reason}). This would kill a legitimate reply.\n"
                        f"  reply: {reply!r}"
                    ),
                )


class DeterministicAugmentDoesNotStompConfidential(unittest.TestCase):
    """The deterministic event layer must stay out of the way of a confidential
    question: it must NOT inject property_unavailable / tour_requested (which
    would auto-respond or mis-route) and must NOT strip a model-emitted
    needs_user_input:confidential event."""

    CONFIDENTIAL_QUESTIONS = [
        "Who is the client?",
        "Can you disclose the tenant name before we share pricing?",
        "What is their credit and intended use?",
        "whos ur clietn??",
        "Client?",
        "WHO IS THE END USER ON THIS DEAL?",
        "Reckon you could tell us who the covenant is before we quote?",
        "Send me the asking rent and let me know who your client is.",
        "Before pricing, I need to know the tenant.",
        "Can you confirm the user for this requirement?",
        "What's their budget and are they approved?",
        "I can't quote without knowing who's behind this - who is it?",
        "Name the tenant and I'll send pricing.",
        "Who are you representing on this search?",
        "Need the covenant strength and company name before I engage.",
    ]

    def _event_types(self, proposal):
        return {(e or {}).get("type") for e in (proposal.get("events") or [])}

    def test_confidential_questions_do_not_get_stomped(self):
        for q in self.CONFIDENTIAL_QUESTIONS:
            with self.subTest(q=q):
                # Simulate the LLM having correctly flagged the confidential
                # question. The deterministic augment must preserve it.
                proposal = {
                    "events": [
                        {"type": "needs_user_input", "reason": "confidential",
                         "question": q},
                    ],
                    "response_email": "",
                }
                out = _augment_events_with_deterministic_signals(
                    proposal, _inbound(q)
                )
                types = self._event_types(out)
                self.assertNotIn(
                    "property_unavailable", types,
                    msg=f"Confidential question misrouted to property_unavailable: {q!r}",
                )
                self.assertNotIn(
                    "tour_requested", types,
                    msg=f"Confidential question misrouted to tour_requested: {q!r}",
                )
                self.assertIn(
                    "needs_user_input", types,
                    msg=f"Deterministic layer dropped the confidential flag for: {q!r}",
                )


class ConfidentialNearMissesDoNotTripUnavailable(unittest.TestCase):
    """The two declared near-misses must NOT be treated as the confidential
    event nor as property_unavailable by the deterministic layer."""

    NEAR_MISSES = [
        # generic requirements already in the campaign brief (legit info request)
        "Just to confirm the brief - what size and ceiling height is the client "
        "after? Want to make sure the space fits before I send options.",
        # tour attendees after tour lane disabled (scheduling logistics, not identity)
        "Who will be attending the tour, and how many people should I expect?",
    ]

    def test_near_misses_not_flagged_unavailable(self):
        for text in self.NEAR_MISSES:
            with self.subTest(text=text[:40]):
                proposal = {"events": [], "response_email": ""}
                out = _augment_events_with_deterministic_signals(
                    proposal, _inbound(text)
                )
                types = {(e or {}).get("type") for e in (out.get("events") or [])}
                self.assertNotIn(
                    "property_unavailable", types,
                    msg=(
                        "FALSE POSITIVE: near-miss broker message was flagged "
                        f"property_unavailable by the deterministic layer: {text!r}"
                    ),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
