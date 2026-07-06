"""Pressure test: broker_new_property_referral SAFETY guards.

EVENT: broker_new_property_referral — a broker replies that the target property
is not a fit / unavailable and points at a DIFFERENT ("alternate") property, OR
volunteers another building they represent.

stopIf (the safety line we must never cross):
  1. alternate property is added to the sheet WITHOUT human review
  2. original row is OVERWRITTEN with the new address

Where the decision lives:
  - Detecting that a raw broker message *is* a new-property referral is done by the
    LLM (see ai_processing.py prompt, event type "new_property"). There is NO
    deterministic text classifier for it — so raw-phrasing -> event is LLM-only.
  - The DETERMINISTIC SAFETY layer around that LLM event is testable and is what
    actually enforces the stopIf conditions:
      * notification_payloads.build_new_property_suggested_email(...)  -> rowNumber
        is ALWAYS None: the alternate becomes a fresh first-touch draft, never an
        update targeting the original row. (guards stopIf #2 "overwrite original")
      * processing.py new_property handler creates an ACTION_NEEDED
        "pending_approval" notification with row_number=None instead of writing a
        row. (guards stopIf #1 "added without review")  [asserted indirectly here]
      * notification_payloads.should_skip_original_reply_for_new_property_referral /
        sanitize_new_property_referral_response route the reply so we don't auto-
        message the wrong party.
      * ai_processing._augment_events_with_deterministic_signals never FABRICATES a
        new_property event from raw text (so a signature/out-of-geo mention can't be
        silently promoted) and never DROPS an LLM new_property event nor moves the
        alternate address onto the original row.

This suite drives those REAL functions on 18 messy real-threat phrasings and 6
near-miss controls. External boundaries (Firestore/_fs, Sheets, Graph) are never
touched: these deterministic guards are pure, so no patching is required.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.ai_processing import (
    _augment_events_with_deterministic_signals as augment_events,
)
from email_automation.notification_payloads import (
    build_new_property_suggested_email,
    sanitize_new_property_referral_response,
    should_skip_original_reply_for_new_property_referral,
)


def convo(inbound_text: str):
    """Minimal conversation ending in the broker's inbound reply."""
    return [
        {"direction": "outbound", "content": "Following up on the property and a possible tour."},
        {"direction": "inbound", "content": inbound_text},
    ]


# ---------------------------------------------------------------------------
# 18 real-threat phrasings. Each is the broker's raw message PLUS the plausible
# new_property event the LLM would emit for it (address/city/email extracted).
# `diff_email` marks whether the referral hands off to a DIFFERENT contact.
# ---------------------------------------------------------------------------
ORIGINAL_BROKER = "sam@brokerage.com"

REAL_THREAT = [
    # (label, raw_text, event)
    ("seed_not_a_fit",
     "This one is not a fit, but 123 Replacement Way may work.",
     {"type": "new_property", "address": "123 Replacement Way", "city": "", "email": ""}),
    ("seed_another_building",
     "We also have another building nearby with more docks.",
     {"type": "new_property", "address": "Another building nearby", "city": "", "email": ""}),
    ("seed_attached_alternate",
     "Please look at the attached alternate property.",
     {"type": "new_property", "address": "[TBD] attached alternate property", "city": "", "email": ""}),
    ("terse",
     "not this one. try 400 Elm.",
     {"type": "new_property", "address": "400 Elm", "city": "", "email": ""}),
    ("verbose_handoff",
     ("Unfortunately the space at the current building has been spoken for and is "
      "no longer available. That said, my colleague Joe handles a comparable "
      "warehouse over at 900 Industrial Pkwy in Reno — you can reach him directly "
      "at joe@otherco.com and he can walk you through availability."),
     {"type": "new_property", "address": "900 Industrial Pkwy", "city": "Reno",
      "email": "joe@otherco.com", "contactName": "Joe"}),
    ("typo",
     "this bldg wont wrok for yur client but 55 Dock Rd mite, lmk",
     {"type": "new_property", "address": "55 Dock Rd", "city": "", "email": ""}),
    ("partial_vague",
     "Not a fit. We do have something else opening up soon.",
     {"type": "new_property", "address": "[TBD] something else opening up", "city": "", "email": ""}),
    ("multi_intent",
     ("Can you confirm you still want the 20k SF? Also this one is office-heavy so "
      "probably not a fit — but 200 Commerce Dr is a true warehouse that works."),
     {"type": "new_property", "address": "200 Commerce Dr", "city": "", "email": ""}),
    ("quoted_history",
     ("Yes still available.\n\n> On Mon you wrote:\n> Is this a fit?\n\n"
      "Actually scratch that, it just leased. Look at 12 Cargo Ct instead."),
     {"type": "new_property", "address": "12 Cargo Ct", "city": "", "email": ""}),
    ("regional",
     "That estate's let, but we've a cracking unit going at 8 Quay Road, give us a shout.",
     {"type": "new_property", "address": "8 Quay Road", "city": "", "email": ""}),
    ("all_caps",
     "THIS IS NO LONGER AVAILABLE. TRY 9 ELM INSTEAD.",
     {"type": "new_property", "address": "9 Elm", "city": "", "email": ""}),
    ("with_signature",
     ("Signed a lease on this one last week. But 77 Rail St is open — reach Amy at "
      "amy@realty.com.\n\n--\nDan Broker\nAcme Realty | 100 HQ Plaza, Dallas TX\n(555) 123-4567"),
     {"type": "new_property", "address": "77 Rail St", "city": "",
      "email": "amy@realty.com", "contactName": "Amy"}),
    ("conflicting_old_quote",
     ("We had quoted $6.50/SF here but that space is gone now. The replacement at "
      "301 Depot is $7.25/SF."),
     {"type": "new_property", "address": "301 Depot", "city": "", "email": ""}),
    ("below_is_only_space",
     "Below is the only current space we have: https://listings.example.com/450-Freight",
     {"type": "new_property", "address": "[TBD] 450 Freight", "city": "",
      "email": "", "link": "https://listings.example.com/450-Freight"}),
    ("heres_an_alternative",
     "Here's an alternative location that should check your boxes: 61 Terminal Ave.",
     {"type": "new_property", "address": "61 Terminal Ave", "city": "", "email": ""}),
    ("explicit_handoff_other_broker",
     ("I don't handle that building anymore — no longer represent the property. "
      "Please contact our other broker Priya at priya@newfirm.com about 5 Loop Rd."),
     {"type": "new_property", "address": "5 Loop Rd", "city": "",
      "email": "priya@newfirm.com", "contactName": "Priya"}),
    ("requirements_mismatch_referral",
     ("This space is too office-heavy as opposed to a true warehouse so it does not "
      "meet your client's requirements. 15 Freightliner Way is a better match."),
     {"type": "new_property", "address": "15 Freightliner Way", "city": "", "email": ""}),
    ("fully_leased_referral",
     "That one's fully leased. Check out 88 Distribution Blvd, similar specs.",
     {"type": "new_property", "address": "88 Distribution Blvd", "city": "", "email": ""}),
]

# ---------------------------------------------------------------------------
# Near-miss controls: these must NOT cause the deterministic layer to fabricate
# a new_property event (which would push an alternate toward the sheet).
# ---------------------------------------------------------------------------
NEAR_MISS = [
    ("signature_only_address",
     "Thanks, will circulate this internally and revert.\n\n--\n"
     "Jane Doe, SIOR\nAcme Realty | 789 Signature Blvd, Chicago IL 60601\n(312) 555-0199"),
    ("disclaimer_footer_address",
     "Confirmed, still available.\n\nCONFIDENTIALITY NOTICE: This email from 1 Corporate "
     "Center, Suite 500, Atlanta GA is intended only for the addressee."),
    ("out_of_geo_not_a_fit",
     "That building is not a fit for your client — it's outside your target area."),
    ("out_of_requirements",
     "This won't be a good fit for your client; it fails your client's requirements on clear height."),
    ("plain_available",
     "Yes, still available. Happy to send the flyer and set up a tour."),
    ("just_a_question",
     "Quick question — what total SF does your client actually need before I dig further?"),
]


class TestNewPropertyReferralAugmentationPreserves(unittest.TestCase):
    """Deterministic augmentation must PRESERVE the LLM new_property event
    (so the alternate reaches the review queue) and must NOT overwrite the
    original row by moving the alternate address anywhere else."""

    def test_new_property_event_survives_every_phrasing(self):
        for label, text, event in REAL_THREAT:
            with self.subTest(phrasing=label):
                proposal = {"events": [dict(event)], "response_email": "Thanks!"}
                out = augment_events(proposal, convo(text))
                types = [e.get("type") for e in out["events"]]
                self.assertIn(
                    "new_property", types,
                    f"[{label}] deterministic layer DROPPED the new_property referral -> "
                    f"alternate would never reach review. events={types}",
                )
                # the alternate address must remain on the new_property event and must
                # NOT have been copied onto any property_unavailable event (overwrite risk)
                np = next(e for e in out["events"] if e.get("type") == "new_property")
                self.assertEqual(
                    np.get("address"), event["address"],
                    f"[{label}] alternate address mutated on the new_property event",
                )
                for e in out["events"]:
                    if e.get("type") == "property_unavailable":
                        self.assertNotIn(
                            event["address"], str(e.values()),
                            f"[{label}] alternate address leaked onto property_unavailable "
                            f"event (original-row overwrite risk)",
                        )

    def test_multi_event_conflict_keeps_alternate_drops_close(self):
        # close_conversation must yield, new_property must survive
        proposal = {"events": [
            {"type": "close_conversation"},
            {"type": "tour_requested"},
            {"type": "new_property", "address": "9 Elm", "city": "X", "email": "j@x.com"},
        ]}
        out = augment_events(proposal, convo("That space is no longer available, but 9 Elm may work."))
        types = [e.get("type") for e in out["events"]]
        self.assertIn("new_property", types)
        self.assertNotIn("close_conversation", types)


class TestNearMissNoFabrication(unittest.TestCase):
    """The deterministic layer must never CREATE a new_property event from raw text.
    A signature/disclaimer address or an out-of-geo mention must not be promoted."""

    def test_near_miss_never_fabricates_new_property(self):
        for label, text in NEAR_MISS:
            with self.subTest(near_miss=label):
                proposal = {"events": [], "response_email": "Thanks!"}
                out = augment_events(proposal, convo(text))
                types = [e.get("type") for e in out["events"]]
                self.assertNotIn(
                    "new_property", types,
                    f"[{label}] deterministic layer FABRICATED a new_property event from a "
                    f"near-miss -> an alternate/none-property would head toward the sheet. events={types}",
                )


class TestSuggestedEmailNeverOverwritesRow(unittest.TestCase):
    """build_new_property_suggested_email must never target the original row:
    rowNumber is None so the alternate is a fresh first-touch, not an update.
    This is the hard guard for stopIf #2 (original row overwritten)."""

    def test_rownumber_always_none(self):
        for label, _text, event in REAL_THREAT:
            with self.subTest(phrasing=label):
                payload = build_new_property_suggested_email(
                    address=event.get("address", ""),
                    city=event.get("city", ""),
                    to_email=event.get("email", "") or ORIGINAL_BROKER,
                    contact_name=event.get("contactName"),
                    referrer_name="Sam",
                    client_id="client-1",
                )
                self.assertIsNone(
                    payload.get("rowNumber"),
                    f"[{label}] suggested-email payload carried a rowNumber "
                    f"({payload.get('rowNumber')!r}) -> could overwrite the original row",
                )
                # subject/body describe the ALTERNATE, never a sheet cell write
                self.assertIn("body", payload)
                self.assertIsInstance(payload.get("to"), list)


class TestReplyRoutingSafety(unittest.TestCase):
    """When the referral hands off to a DIFFERENT contact, the original broker must
    not be auto-replied as if they were the new-property contact."""

    def test_different_contact_skips_original_reply(self):
        for label, _text, event in REAL_THREAT:
            new_email = (event.get("email") or "").strip().lower()
            if not new_email or new_email == ORIGINAL_BROKER:
                continue  # same-contact referrals are covered below
            with self.subTest(phrasing=label):
                self.assertTrue(
                    should_skip_original_reply_for_new_property_referral(
                        original_contact_email=ORIGINAL_BROKER,
                        new_property_email=new_email,
                    ),
                    f"[{label}] different-contact referral did NOT skip the original reply",
                )
                proposal = {
                    "response_email": "Hi Sam, about your building...",
                    "events": [dict(event)],
                }
                out = sanitize_new_property_referral_response(
                    proposal, original_contact_email=ORIGINAL_BROKER
                )
                self.assertTrue(
                    out.get("skip_response"),
                    f"[{label}] sanitize did not set skip_response for different contact",
                )
                self.assertIsNone(
                    out.get("response_email"),
                    f"[{label}] sanitize left a response_email addressed to the wrong party",
                )

    def test_same_contact_keeps_reply(self):
        # Same broker volunteering another building: we SHOULD still be able to
        # reply to them; sanitize must not nuke the response.
        event = {"type": "new_property", "address": "55 Dock Rd", "email": ""}
        proposal = {"response_email": "Thanks Sam!", "events": [dict(event)]}
        out = sanitize_new_property_referral_response(
            proposal, original_contact_email=ORIGINAL_BROKER
        )
        self.assertNotEqual(out.get("skip_response"), True,
                            "same-contact referral wrongly skipped the reply")
        self.assertEqual(out.get("response_email"), "Thanks Sam!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
