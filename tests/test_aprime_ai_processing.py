"""
Surface A′ regression tests for email_automation/ai_processing.py.

Each test pins a confirmed live-model misread (M01–M37) from
docs/release-safety/surface-aprime-real-ai-findings.md, driving the REAL
deterministic guard functions (or the prompt-builder) with the VERBATIM misread
phrasing. Deterministic guards are exercised as pure functions; prompt-level
fixes are pinned by asserting the mechanical property (the right text/rule
reaches the prompt builder), never a live-model call.

No Firestore / Sheets / Graph / OpenAI network calls happen: guards are pure over
(proposal, conversation); the one prompt-level test mocks the OpenAI client.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing as ai
from email_automation.ai_processing import (
    _augment_events_with_deterministic_signals as augment,
    _augment_proposal_with_deterministic_extractions as augment_extractions,
    _extract_rent_sf_yr_from_text,
    _strip_quoted_history,
    _resolve_greeting_first_name,
    _looks_like_tour_slot_reply,
    _looks_like_requirements_mismatch_nonviable,
)
from email_automation.column_config import detect_column_mapping
from email_automation.tour_scheduling import (
    TOUR_INTENT_ACTIONABLE,
    TOUR_INTENT_COURTESY,
    classify_tour_intent,
    looks_like_tour_scheduling_reply,
    subject_bound_tour_segments,
)


def _inbound(text):
    return [{"direction": "inbound", "content": text}]


def _convo(outbound, inbound):
    return [
        {"direction": "outbound", "content": outbound},
        {"direction": "inbound", "content": inbound},
    ]


def _types(proposal):
    return [(e or {}).get("type") for e in (proposal.get("events") or [])]


def _fires_pu(text, target_anchor=None, seed=None, **kw):
    proposal = {"events": list(seed or [])}
    out = augment(proposal, _inbound(text), target_anchor=target_anchor, **kw)
    return "property_unavailable" in _types(out)


# ===========================================================================
# FIX-01 — ground unavailable_patterns to the TARGET + negation-aware.
# ===========================================================================
class FIX01_TargetGroundedTerminal(unittest.TestCase):
    def test_m03_other_property_leased_target_still_available(self):
        text = ("Quick update, Baylor -- 6200 Chemical Rd just leased. 4501 Hollins "
                "Ferry is still available though, and ownership is motivated. Want me "
                "to send updated pricing?")
        self.assertFalse(_fires_pu(text, target_anchor="4501 Hollins Ferry Rd, Baltimore"))

    def test_m06_negated_office_heavy_positive_pitch(self):
        text = ("Unlike most buildings in this corridor, this one is NOT office-heavy "
                "-- it's true warehouse throughout, with three drive-ins and 28' clear. "
                "Honestly a rare find and definitely worth a look for your client. When "
                "can they walk it?")
        seed = [{"type": "tour_requested", "reason": "", "question": "When can they walk it?"}]
        out = augment({"events": list(seed)}, _inbound(text), target_anchor="4501 Hollins Ferry Rd")
        self.assertNotIn("property_unavailable", _types(out))
        self.assertIn("tour_requested", _types(out))

    def test_m06_requirements_helper_negation_aware(self):
        self.assertFalse(_looks_like_requirements_mismatch_nonviable(
            "this one is NOT office-heavy -- it's true warehouse throughout"))
        # positive control: real office-heavy mismatch still fires
        self.assertTrue(_looks_like_requirements_mismatch_nonviable(
            "This is more office-heavy and not a true warehouse fit."))

    def test_m15_ancillary_trailer_lot_leased(self):
        text = ("Happy to give your client a tour of the space -- Wednesday or Thursday "
                "both work. Separately, could you send over your client's requirements "
                "one-pager and let me know whether they'd need outside trailer storage? "
                "Ownership asks because the trailer lot is leased separately.")
        seed = [{"type": "tour_requested", "reason": "", "question": "tour"}]
        out = augment({"events": list(seed)}, _inbound(text), target_anchor="4501 Hollins Ferry Rd")
        self.assertNotIn("property_unavailable", _types(out))
        self.assertIn("tour_requested", _types(out))

    def test_m19_comps_reference_shows_well(self):
        text = ("Attached is the flyer - page 2 has a comps table showing what recently "
                "leased along the corridor, so you can see how the asking rate stacks up. "
                "The space itself shows really well.")
        proposal = {"events": [], "response_email": "Hi Rich, could you re-send the flyer?"}
        out = augment(proposal, _inbound(text), target_anchor="4501 Hollins Ferry Rd")
        self.assertNotIn("property_unavailable", _types(out))
        # FIX-03: no injection => response_email must remain live
        self.assertEqual(out.get("response_email"), "Hi Rich, could you re-send the flyer?")

    def test_m20_tour_slot_window_no_longer_available(self):
        text = ("Unfortunately that 10 AM window is no longer available on my end - I got "
                "double-booked. The listing itself is totally fine, nothing has changed "
                "with the space. Could we do 2 PM on Friday instead?")
        seed = [{"type": "tour_requested", "reason": "", "question": "2 PM Friday?"}]
        out = augment({"events": list(seed)}, _inbound(text), target_anchor="4501 Hollins Ferry Rd")
        self.assertNotIn("property_unavailable", _types(out))
        self.assertIn("tour_requested", _types(out))

    def test_m24_other_deal_closed_target_available(self):
        text = ("we just wrapped up a closing on the other side of town (9 Center Drive, "
                "fully leased now, that one dragged on forever) but the building is very "
                "much still available, the owner is motivated, we're asking $8.75 per "
                "square foot per year on a NNN basis")
        self.assertFalse(_fires_pu(text, target_anchor="4501 Hollins Ferry Rd, Baltimore"))

    def test_m25_forwarded_block_does_not_terminalize(self):
        text = ("Bottom line: still available, 42,000 SF at $8.75/SF NNN.\n\n"
                "---------- Forwarded message ----------\n"
                "From: Gary Holt <gary@harborpointcre.com>\n"
                "Cc: leasing-all@harborpointcre.com; tzhang@oldtenantco.com\n"
                "the Hollins Ferry unit is vacant, broom-clean and ready. Separately, "
                "2201 Pulaski Hwy is fully leased as of last month so take it off your list.")
        self.assertFalse(_fires_pu(text, target_anchor="4501 Hollins Ferry Rd"))

    def test_terminal_shares_sentence_with_size_price_still_fires(self):
        # CodeRabbit PR#15: a size/price figure sharing the terminal sentence
        # ("42,000 SF at $8.75/SF") must NOT be read as a competing street
        # address and mask the property_unavailable signal on the TARGET listing.
        # The old raw-3-6-digit proxy treated the grouped "000" of "42,000" as a
        # competing address and dropped the terminal.
        text = "It's been leased, 42,000 SF at $8.75/SF NNN, sorry."
        self.assertTrue(_fires_pu(text, target_anchor="699 Industrial Park Dr"))
        # A genuinely competing STREET address in the terminal sentence must still
        # be respected (no false terminal on the target).
        self.assertFalse(_fires_pu(
            "6200 Chemical Rd has been leased; the space is still available.",
            target_anchor="699 Industrial Park Dr"))


# ===========================================================================
# FIX-07 — forwarded-message markers in _strip_quoted_history.
# ===========================================================================
class FIX07_StripForwarded(unittest.TestCase):
    def test_gmail_forwarded_marker(self):
        raw = ("Bottom line: still available.\n\n"
               "---------- Forwarded message ----------\n"
               "From: Gary Holt\n2201 Pulaski Hwy is fully leased.")
        stripped = _strip_quoted_history(raw)
        self.assertEqual(stripped, "Bottom line: still available.")
        self.assertNotIn("fully leased", stripped.lower())

    def test_apple_begin_forwarded_marker(self):
        raw = "New note here.\nBegin forwarded message:\nFrom: X\nold rejected content leased"
        self.assertEqual(_strip_quoted_history(raw), "New note here.")


# ===========================================================================
# FIX-03 — injection resolves contradictions atomically (null response_email).
# ===========================================================================
class FIX03_InjectionNullsResponse(unittest.TestCase):
    def test_genuine_terminal_nulls_response_email(self):
        proposal = {"events": [], "response_email": "Hi, could you re-send the flyer?"}
        out = augment(proposal, _inbound("The building is fully leased."))
        self.assertIn("property_unavailable", _types(out))
        self.assertIsNone(out.get("response_email"))


# ===========================================================================
# FIX-04 — symmetric RETENTION guards for LLM-emitted events.
# ===========================================================================
class FIX04_RetentionGuards(unittest.TestCase):
    def test_m01_alternate_viable_strips_llm_pu(self):
        text = ("One suite is leased but an alternate suite in the same property remains "
                "viable. Suite B is 28,000 SF, 24' clear, three docks and one drive-in.")
        seed = [{"type": "property_unavailable", "reason": "",
                 "notes": "One suite is leased; alternate suite (Suite B) remains available"}]
        out = augment({"events": list(seed)}, _inbound(text))
        self.assertNotIn("property_unavailable", _types(out))

    def test_m02_quoted_pu_stripped(self):
        raw = ("Checking in -- is your client still active in the market? I may have "
               "some new options coming this fall...\n\n"
               "On Wed, Jul 1, 2026 Marcus Reyes wrote:\n"
               "> That suite is no longer available.\n"
               "> We ended up leasing it to a 3PL group.")
        seed = [{"type": "property_unavailable", "reason": "",
                 "notes": "Suite leased to a 3PL group"}]
        out = augment({"events": list(seed)}, _inbound(raw))
        self.assertNotIn("property_unavailable", _types(out))

    def test_m05_quoted_pu_stripped_call_kept(self):
        raw = ("following up on my note below. Do you have 10 minutes for a call this "
               "week?\n\nOn Tue, Jul 1, 2026 Greg Sutton wrote:\n"
               "> We do not have drive-in doors, so it likely will not work.")
        seed = [
            {"type": "property_unavailable", "reason": "", "notes": "Not a fit due to no drive-in doors"},
            {"type": "call_requested", "reason": "", "question": "Do you have 10 minutes for a call this week?"},
        ]
        out = augment({"events": list(seed)}, _inbound(raw))
        self.assertNotIn("property_unavailable", _types(out))
        self.assertIn("call_requested", _types(out))

    def test_m07_wrong_contact_self_referential_stripped(self):
        text = ("Alex Chen here - Miguel forwarded your inquiry over to me and I'm the "
                "right contact for 4501 Hollins Ferry, so you're all set now. The space "
                "is available: 42,000 SF at $8.25/SF/YR NNN.")
        seed = [{"type": "wrong_contact", "reason": "forwarded", "suggestedContact": "Alex Chen"}]
        out = augment({"events": list(seed)}, _inbound(text),
                      contact_name="Alex Chen", sender_email="achen@harborpointcre.com")
        self.assertNotIn("wrong_contact", _types(out))

    def test_m09_quoted_confidential_stripped(self):
        raw = ("All good on our end - flyer with full specs is attached, and the space "
               "is available. Let me know if you need anything else.\n\n"
               "> Before I send anything over - who is your client? Ownership usually asks.")
        seed = [{"type": "needs_user_input", "reason": "confidential",
                 "question": "Before I send anything over - who is your client? Ownership usually asks."}]
        out = augment({"events": list(seed)}, _inbound(raw))
        self.assertNotIn("needs_user_input", _types(out))

    def test_m10_quoted_new_property_stripped(self):
        raw = ("Confirmed - 4501 Hollins Ferry is still available and the flyer is "
               "attached. 42,000 SF, $8.75/SF NNN.\n\n"
               "> We also have 700 Crossfield Court available if this one doesn't work out.")
        seed = [{"type": "new_property", "address": "700 Crossfield Court",
                 "notes": "Mentioned as an alternative if 4501 Hollins Ferry Rd doesn't work out"}]
        out = augment({"events": list(seed)}, _inbound(raw), target_anchor="4501 Hollins Ferry Rd")
        self.assertNotIn("new_property", _types(out))

    def test_m16_quoted_tour_offer_stripped(self):
        raw = ("Just acknowledging I got your note -- I'll pull the updated spec sheet "
               "and get back to you by end of day.\n\n"
               "On Wed, Jul 1 Tom Merrick wrote:\n"
               "> Happy to schedule a tour next week if your client wants to walk the space.\n"
               "> Let me know.")
        seed = [{"type": "tour_requested", "reason": "",
                 "question": "Happy to schedule a tour next week if your client wants to walk the space."}]
        out = augment({"events": list(seed)}, _inbound(raw))
        self.assertNotIn("tour_requested", _types(out))

    def test_m17_quoted_tour_offer_stripped(self):
        raw = ("Got your voicemail -- I'll call you back shortly.\n\n"
               "> We can show it Friday afternoon, or Monday morning if that works "
               "better for your client.")
        seed = [{"type": "tour_requested", "reason": "",
                 "question": "We can show it Friday afternoon, or Monday morning if that works better for your client."}]
        out = augment({"events": list(seed)}, _inbound(raw))
        self.assertNotIn("tour_requested", _types(out))

    def test_m21_quoted_reschedule_stripped(self):
        raw = ("Quick admin note before anything else - our office moved to Suite 400, "
               "same building and phone. I'll follow up separately once I hear back from "
               "the owner.\n\n"
               "On Wed, Jul 2, 2026 at 4:05 PM Marcus Reyes wrote:\n"
               "> 10 AM does not work; can you do 2 PM instead?")
        seed = [{"type": "tour_requested", "reason": "scheduling",
                 "question": "Marcus indicated 10 AM does not work and asked if you can do 2 PM instead."}]
        out = augment({"events": list(seed)}, _inbound(raw))
        self.assertNotIn("tour_requested", _types(out))

    def test_m27_quoted_optout_stripped(self):
        raw = ("Confirming receipt Baylor - I'll have the full package over to you "
               "tomorrow morning.\n\n-----Original Message-----\n"
               "Please remove me from your distribution list, I am not interested in "
               "these mailings and do not contact me again.")
        seed = [{"type": "contact_optout", "reason": "do_not_contact",
                 "notes": "Forwarded message from Rick Delgado requesting removal from distribution list"}]
        out = augment({"events": list(seed)}, _inbound(raw))
        self.assertNotIn("contact_optout", _types(out))


class FIX11_TourCourtesyInvitationRetention(unittest.TestCase):
    """A generic broker sign-off is not an actionable tour request."""

    def _types_after_model_tour(self, text):
        proposal = {
            "events": [{
                "type": "tour_requested",
                "reason": "tour_offer",
                "question": text,
                "suggestedEmail": "Would Tuesday work?",
            }],
            "response_email": None,
        }
        return _types(augment(proposal, _inbound(text)))

    def test_exact_flyer_canary_invitation_is_not_a_tour_event(self):
        text = (
            "Hi John, the suite is available. It contains 22,400 square feet, "
            "the asking rent is $12.95 per square foot per year NNN, and estimated "
            "operating expenses are $2.85 per square foot. The property flyer and "
            "listing details are here: https://sitesiftai.com/help. Please let me "
            "know if you need a tour. Best, Jordan"
        )
        self.assertNotIn("tour_requested", self._types_after_model_tour(text))

    def test_generic_tour_invitation_signoffs_are_not_events(self):
        invitations = (
            "Let me know if you want a tour.",
            "Please let me know if your client needs a showing.",
            "Let me know if you'd like to schedule a walkthrough.",
            "Feel free to let me know if you want to see it.",
            "The flyer is dated Tuesday. Please let me know if you need a tour.",
            "I can send the flyer Tuesday. Let me know if you want a tour.",
            "Please let me know if you need a tour. I am OOO Tuesday.",
            "Tours are available upon request.",
            "Happy to arrange a tour if useful.",
            "Virtual tours are available at https://example.com/virtual.",
            "A 360 tour is available in the flyer.",
            "The video walkthrough is available here.",
            "The virtual showing is available on request.",
            "Would you like to see the space in the attached photos?",
            "Can you see the virtual tour link?",
            "I can arrange a tour if needed.",
            "A walkthrough is available online.",
            "The flyer arrives Tuesday at 2 PM, so let me know if your client wants to schedule a tour.",
            "I can schedule a tour if you want.",
            "We can arrange a showing upon request.",
            "An online showing is available Tuesday.",
        )
        for text in invitations:
            with self.subTest(text=text):
                self.assertEqual(TOUR_INTENT_COURTESY, classify_tour_intent(text))
                self.assertNotIn("tour_requested", self._types_after_model_tour(text))

    def test_direct_or_concrete_tour_offers_remain_actionable(self):
        offers = (
            "Would you like to see the space?",
            "Happy to show you the suite; when works for you?",
            "Happy to show you the space, when works for you?",
            "Happy to show you the property if Tuesday works for you.",
            "Happy to show you the property whenever works for you.",
            "You are welcome to visit the property Tuesday.",
            "I can let your client into the property Tuesday.",
            "We can accommodate a visit Tuesday.",
            "I can provide access Tuesday.",
            "I can provide access at 2 PM.",
            "You are welcome to visit the property Tuesday with your client.",
            "We can accommodate a visit Tuesday at the property.",
            "I can provide access at 2 PM to the suite.",
            "Tours are available next week.",
            "Can your client tour Tuesday at 2 PM?",
            "Let me know if Tuesday at 2 PM works for a tour.",
            "When would your client like to tour?",
            "Please send two times next week so I can schedule the showing.",
            "What day works for a tour?",
            "Please propose a few windows for a showing.",
            "Let me know if your client can tour.",
            "Are there any dates that work for a walkthrough?",
            "Could your client make a showing next week?",
            "Does Tuesday afternoon work for a walkthrough?",
            "I have Tuesday and Thursday open for tours.",
            "Pick a time for the showing.",
            "Please confirm the Wednesday 10 AM showing.",
            "The owner is offering Tuesday at 3 PM for a walkthrough.",
            "Which of these tour windows works for your client?",
            "Send me your availability and I will arrange the tour.",
            "When should we meet at the property?",
            "Could we meet at the front entrance Tuesday?",
            "Let me know when your client wants to walk the space.",
            "Tour Friday at 2 PM?",
            "Can do a showing Monday.",
            "Keen to arrange a viewing of the unit at your convenience.",
            "We can do a walkthrough Thursday morning.",
            "Virtual tours are available online, but I can show the property Tuesday.",
            "Let me know if you need a tour, but I can show the property Tuesday.",
            "Let me know if your client wants to schedule a tour Tuesday at 2 PM.",
            "Can your client see the property Tuesday afternoon?",
            "Virtual tours are available online, and I can show the property Tuesday.",
            "Virtual tours are available online and I can show the property Tuesday.",
            "Virtual tours are available online, I can show the property Tuesday.",
            "I can show the property Tuesday, so let me know if you need a tour.",
            "Would your client like to see the property?",
            "Can I show you the property Tuesday?",
            "I can show you the property Tuesday in person.",
            "Would your client like a tour?",
            "Do you want a tour?",
            "Does your client want a tour?",
            "Come see the property Tuesday.",
            "The property is available, and I can show it Tuesday.",
            "The suite is ready. I can show it Tuesday.",
            "The building is open and I can walk through it Tuesday.",
            "The property is available. You can see it Tuesday.",
            "This one is a warehouse. You can see it Tuesday.",
            "The current tenant will vacate next month. We can show it Tuesday.",
            "The tenant moves out Friday. You can walk through it Tuesday.",
            "Virtual tours are available online or I can show the property Tuesday.",
            "Virtual tours are available online — I can show the property Tuesday.",
        )
        for text in offers:
            with self.subTest(text=text):
                self.assertEqual(TOUR_INTENT_ACTIONABLE, classify_tour_intent(text))
                self.assertIn("tour_requested", self._types_after_model_tour(text))

    def test_calendar_dated_generic_tour_invitations_are_actionable(self):
        for tour_window in (
            "Tuesday, August 11 at 2:00 PM",
            "Tuesday August 11 at 2 PM",
            "August 11 at 2 PM",
            "Tue, Aug 11, 2026 at 2 PM",
            "Tuesday, Aug. 11 at 2 PM",
            "Tue., Aug. 11, 2026 at 2 p.m",
            "8/11 at 2 PM",
            "August 11th at 2 PM",
        ):
            with self.subTest(tour_window=tour_window):
                text = (
                    "Let me know if your client wants to schedule a tour "
                    f"{tour_window}."
                )
                self.assertEqual(TOUR_INTENT_ACTIONABLE, classify_tour_intent(text))
                self.assertTrue(looks_like_tour_scheduling_reply(text))
                self.assertIn("tour_requested", self._types_after_model_tour(text))

    def test_calendar_date_does_not_make_virtual_or_artifact_tour_copy_actionable(self):
        for text in (
            "Let me know if your client wants to schedule a tour Tuesday, August 11 at 2:00 PM via Zoom.",
            "Let me know if your client wants to schedule a tour Tuesday, August 11 at 2:00 PM online.",
            "Let me know if your client wants to schedule a tour Tuesday, August 11 at 2:00 PM in the financial model.",
            "The flyer is dated Tuesday, August 11 at 2:00 PM. Please let me know if you need a tour.",
            "A tour video is available Tuesday, August 11 at 2:00 PM.",
            "Let me know if your client wants to schedule a tour August 0 at 2 PM.",
            "Let me know if your client wants to schedule a tour August 32 at 2 PM.",
            "Let me know if your client wants to schedule a tour 13/11 at 2 PM.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. via Zoom.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. online.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. in the financial model.",
        ):
            with self.subTest(text=text):
                self.assertEqual(TOUR_INTENT_COURTESY, classify_tour_intent(text))
                self.assertFalse(looks_like_tour_scheduling_reply(text))
                self.assertNotIn("tour_requested", self._types_after_model_tour(text))

    def test_dotted_clock_tour_offer_still_splits_a_true_next_sentence(self):
        tour_sentence = (
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, "
            "2026 at 2 p.m."
        )
        for next_sentence in (
            "The rent schedule is confirmed.",
            "In the meantime, the rent schedule is attached.",
            "Online pricing is available.",
            "Via email, I sent the flyer.",
            "in the meantime, the rent schedule is attached.",
            "online pricing is available.",
            "via email, I sent the flyer.",
        ):
            with self.subTest(next_sentence=next_sentence):
                text = f"{tour_sentence} {next_sentence}"
                self.assertEqual(
                    [tour_sentence],
                    subject_bound_tour_segments(text),
                )
                self.assertEqual(TOUR_INTENT_ACTIONABLE, classify_tour_intent(text))
                self.assertIn("tour_requested", self._types_after_model_tour(text))

    def test_subject_bound_helper_matches_physical_and_rejects_virtual_only(self):
        for text in (
            "The property is available, and I can show it Tuesday.",
            "The suite is ready. I can show it Tuesday.",
            "The building is open and I can walk through it Tuesday.",
            "Would your client like a tour?",
            "Do you want a tour?",
            "Does your client want a tour?",
            "Come see the property Tuesday.",
            "The property is available. You can see it Tuesday.",
            "This one is a warehouse. You can see it Tuesday.",
            "You are welcome to visit the property Tuesday.",
            "I can let your client into the property Tuesday.",
            "We can accommodate a visit Tuesday.",
            "I can provide access Tuesday.",
            "I can provide access at 2 PM.",
            "You are welcome to visit the property Tuesday with your client.",
            "We can accommodate a visit Tuesday at the property.",
            "I can provide access at 2 PM to the suite.",
            "The current tenant will vacate next month. We can show it Tuesday.",
            "The tenant moves out Friday. You can walk through it Tuesday.",
            "Virtual tours are available online or I can show the property Tuesday.",
            "Virtual tours are available online — I can show the property Tuesday.",
            "Let me know if your client wants to schedule a tour Tuesday at 2 PM.",
            "The Tuesday slot works for us.",
            "The 2 PM slot is confirmed.",
            "The requested tour slot works.",
        ):
            with self.subTest(text=text):
                self.assertTrue(looks_like_tour_scheduling_reply(text))

        self.assertFalse(
            looks_like_tour_scheduling_reply("Virtual tours are available online.")
        )

        for text in (
            "The rent review at that time is confirmed.",
            "The pricing call at that slot works for us.",
            "The financial model at that time is unavailable.",
            "The floor plan at that slot is confirmed.",
            "The lease schedule at that time doesn't work.",
            "Rent is confirmed at that time.",
            "Pricing does not work at that slot.",
            "Model review is unavailable at that time.",
            "Floor plan is confirmed at that slot.",
            "Lease terms work for us at that time.",
            "The tour report at that time is confirmed.",
            "We reviewed when the tenant vacates. I can show it Tuesday.",
            "We discussed when the tenant moves out. I can show it Tuesday.",
            "The schedule notes when the tenant vacates. I can show it Tuesday.",
            "The timeline records when the tenant moves out. I can show it Tuesday.",
            "We discussed the tour schedule for when the tenant moves out. I can show it Tuesday.",
            "The tour timeline notes when the tenant vacates. I can show it Tuesday.",
            "The pricing call is at 2 PM. That time works.",
            "The rent review is scheduled for Tuesday. That time is confirmed.",
            "The pricing call is confirmed. That no longer works.",
            "The lease meeting is at 10 AM. That slot is unavailable.",
            "I can provide access to the tenant schedule after the tenant moves out. I can show it Tuesday.",
            "I can let them review the floor plan after the tenant moves out. I can show it Tuesday.",
            "I can visit the pricing model once the tenant vacates. I can show it Tuesday.",
            "I can show you the property Tuesday online.",
            "I can show you the property Tuesday in the financial model.",
            "Would your client like to see the property on the listing page?",
            "Let me know if your client wants to schedule a tour Tuesday via Zoom.",
            "Let me know if your client wants to schedule a tour Tuesday online.",
            "Let me know if your client wants to schedule a tour Tuesday in the financial model.",
            "You are welcome to visit the property Tuesday to review the lease.",
            "I can let your client into the property Tuesday for the pricing call.",
            "We can accommodate a visit Tuesday to discuss pricing.",
            "I can provide access at 2 PM to the floor plan.",
        ):
            with self.subTest(text=text):
                self.assertFalse(looks_like_tour_scheduling_reply(text))

    def test_unknown_tour_report_is_preserved_for_processing_fail_closed_boundary(self):
        text = "The tour report is available Tuesday."

        self.assertNotEqual(TOUR_INTENT_ACTIONABLE, classify_tour_intent(text))
        self.assertFalse(looks_like_tour_scheduling_reply(text))
        self.assertIn("tour_requested", self._types_after_model_tour(text))

    def test_fresh_bare_time_model_event_is_preserved_without_inventing_ai_context(self):
        text = "2 PM."
        proposal = {
            "events": [{
                "type": "tour_requested",
                "reason": "tour_slot_reply",
                "question": text,
            }],
        }

        out = augment(proposal, _inbound(text))

        self.assertIn("tour_requested", _types(out))

# ===========================================================================
# FIX-09 — new_property notes-contradiction post-hoc guard.
# ===========================================================================
class FIX09_NewPropertyContradiction(unittest.TestCase):
    def _strip_np(self, notes, address="123 Somewhere Rd", text="Here is another option."):
        seed = [{"type": "new_property", "address": address, "notes": notes}]
        out = augment({"events": list(seed)}, _inbound(text))
        return "new_property" not in _types(out)

    def test_m11_not_a_fit(self):
        self.assertTrue(self._strip_np(
            "Retail strip suite; broker notes it's likely not a fit for industrial/warehouse need"))

    def test_m12_not_the_target_build_to_suit(self):
        self.assertTrue(self._strip_np(
            "Mentioned as tenant's new build-to-suit location (not the target property)"))

    def test_m24_fully_leased_now(self):
        self.assertTrue(self._strip_np(
            "Mentioned as a separate property they just closed; fully leased now",
            address="9 Center Drive"))

    def test_m25_fully_leased_not_available(self):
        self.assertTrue(self._strip_np(
            "Mentioned as fully leased as of last month (not available).",
            address="2201 Pulaski Hwy"))

    def test_m29_not_on_offer(self):
        self.assertTrue(self._strip_np(
            "Eastpoint; Dana said it is for a separate client and not on offer",
            address="800 Broening Hwy"))

    def test_legit_referral_preserved(self):
        # Control: a clean referral with benign notes must SURVIVE.
        self.assertFalse(self._strip_np(
            "Comparable warehouse with more docks; owner motivated"))


# ===========================================================================
# FIX-10 — subject attribution / temporary-absence for optout/wrong_contact.
# ===========================================================================
class FIX10_SubjectAttribution(unittest.TestCase):
    def test_m08_out_of_office_not_wrong_contact(self):
        text = ("I am out of the office until Monday, July 14 with limited access to "
                "email. For urgent matters, please contact my assistant Mara Nguyen at "
                "mnguyen@bayviewindustrial.com or 410-555-0142.")
        seed = [{"type": "wrong_contact", "reason": "forwarded", "suggestedContact": "Mara Nguyen",
                 "suggestedEmail": "mnguyen@bayviewindustrial.com"}]
        out = augment({"events": list(seed)}, _inbound(text), contact_name="Dana Brooks")
        self.assertNotIn("wrong_contact", _types(out))

    def test_m28_machine_banner_third_party_optout_stripped(self):
        text = ("[AUTOMATED THREAD NOTICE - HarborPoint MailGuard] tabbott@harborpointcre.com "
                "has OPTED OUT of this correspondence. [END NOTICE] Dana's note: Baylor - "
                "ignore the robo-banner above, our IT added it for Tom's inbox rules. Space "
                "is still available: 42,000 SF, $8.75/SF NNN, OpEx $2.10.")
        seed = [{"type": "contact_optout", "reason": "unsubscribe",
                 "email": "tabbott@harborpointcre.com", "contactName": "Tom Abbott"}]
        out = augment({"events": list(seed)}, _inbound(text),
                      contact_name="Dana Brooks", sender_email="dana@harborpointcre.com")
        self.assertNotIn("contact_optout", _types(out))


# ===========================================================================
# FIX-13 / FIX-14 — greeting name resolution.
# ===========================================================================
class FIX13_14_Greeting(unittest.TestCase):
    def test_m30_mapped_name_disagrees_with_sender_neutral(self):
        got = _resolve_greeting_first_name(
            "Jordan Lee", sender_email="pwong@keystoneindustrial.com",
            sender_signature_name="Patricia Wong")
        self.assertIsNone(got)

    def test_agreeing_name_resolves(self):
        got = _resolve_greeting_first_name(
            "Patricia Wong", sender_email="pwong@keystoneindustrial.com",
            sender_signature_name="Patricia Wong")
        self.assertEqual(got, "Patricia")

    def test_body_not_used_as_signature_fallback(self):
        # CodeRabbit PR#18 (ai_processing.py:1857): the call site must NOT pass
        # the full inbound body as sender_signature_name. If it did, the raw
        # substring match would spuriously "agree" (mapped first name "Rob"
        # occurs inside "problem"), reviving a stale greeting. With the fixed
        # call site the signature is None, so a disagreeing sender stays neutral.
        body = "The problem is bigger than we expected on the north dock."
        # Hazard being guarded against: feeding the body DOES falsely agree.
        self.assertEqual(
            "Rob",
            _resolve_greeting_first_name(
                "Rob Fields", sender_email="patricia.wong@keystone.com",
                sender_signature_name=body),
        )
        # Post-fix behavior: no signature -> disagreeing sender -> neutral.
        self.assertIsNone(
            _resolve_greeting_first_name(
                "Rob Fields", sender_email="patricia.wong@keystone.com",
                sender_signature_name=None),
        )

    def test_m31_company_name_neutral(self):
        self.assertIsNone(_resolve_greeting_first_name("Colliers International"))

    def test_m32_honorific_stripped(self):
        self.assertEqual(
            _resolve_greeting_first_name("Dr. Angela Marchetti-Kowalski"), "Angela")


# ===========================================================================
# FIX-15 — rent fallback: TI credit is not rent; correct value written.
# ===========================================================================
class FIX15_RentFallback(unittest.TestCase):
    M13_TEXT = ("Still available. We can offer a $2/SF TI credit and the landlord will "
                "consider a month of free rent for a 5-year term. Full specs: 28,500 SF, "
                "$7.95/SF NNN, 21' clear, 3 docks.")

    def test_m13_function_returns_asking_rent_not_ti_credit(self):
        self.assertEqual(_extract_rent_sf_yr_from_text(self.M13_TEXT), "7.95")

    def test_m13_pipeline_writes_795_not_200(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["4501 Hollins Ferry Rd", ""]
        config = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        out = augment_extractions({"updates": []}, rowvals, header, config, _inbound(self.M13_TEXT))
        rent = [u for u in out["updates"] if u["column"] == "Rent/SF /Yr"]
        self.assertEqual([u["value"] for u in rent], ["7.95"])

    def test_llm_provided_correct_rent_not_clobbered(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["4501 Hollins Ferry Rd", ""]
        config = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        proposal = {"updates": [{"column": "Rent/SF /Yr", "value": "7.95", "confidence": 0.9}]}
        out = augment_extractions(proposal, rowvals, header, config, _inbound(self.M13_TEXT))
        rent = [u for u in out["updates"] if u["column"] == "Rent/SF /Yr"]
        self.assertEqual([u["value"] for u in rent], ["7.95"])


# ===========================================================================
# FIX-16 — dollar-less / psf / pdf rent.
# ===========================================================================
class FIX16_RentWidening(unittest.TestCase):
    def test_m33_dollarless_nnn(self):
        self.assertEqual(_extract_rent_sf_yr_from_text("still avail, 8.75 nnn. -b"), "8.75")

    def test_m33_dollarless_a_foot_nnn(self):
        self.assertEqual(_extract_rent_sf_yr_from_text("...8.75 a foot nnn opex like 2.10..."), "8.75")

    def test_psf_fused_token(self):
        self.assertEqual(_extract_rent_sf_yr_from_text("$8.75 psf per annum NNN"), "8.75")

    def test_m35_pdf_manifest_rent(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["2801 Pulaski Hwy", ""]
        config = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        synthetic = _inbound("Here is information about 2801 Pulaski Hwy")
        manifest = [{"name": "flyer.pdf", "text": "Asking Rent: $10.50/SF NNN. 42,000 SF."}]
        out = augment_extractions({"updates": []}, rowvals, header, config, synthetic,
                                  pdf_manifest=manifest)
        rent = [u for u in out["updates"] if u["column"] == "Rent/SF /Yr"]
        self.assertEqual([u["value"] for u in rent], ["10.50"])


# ===========================================================================
# FIX-02 — tour_slot reply signal narrowing (M04).
# ===========================================================================
class FIX02_TourSlotNarrowing(unittest.TestCase):
    M04_TEXT = ("the warehouse component is maybe 3,000 square feet at the back with a "
                "single roll-up, and the rest is finished office space across two floors "
                "-- private offices, conference rooms, a big training room, the works. "
                "Given your client needs 20,000+ SF of functional warehouse for "
                "distribution, I just don't see this one working no matter how you slice it.")

    def test_the_works_idiom_is_not_a_tour_slot_reply(self):
        convo = [
            {"direction": "outbound", "content": "I toured this building myself back in March; happy to set up a tour."},
            {"direction": "inbound", "content": self.M04_TEXT},
        ]
        self.assertFalse(_looks_like_tour_slot_reply(convo, self.M04_TEXT.lower()))

    def test_m04_nonviable_classification_survives(self):
        convo = [
            {"direction": "outbound", "content": "I toured this building myself back in March; happy to set up a tour."},
            {"direction": "inbound", "content": self.M04_TEXT},
        ]
        seed = [{"type": "property_unavailable", "reason": "requirements_mismatch"}]
        out = augment({"events": list(seed)}, convo)
        self.assertIn("property_unavailable", _types(out))
        self.assertNotIn("tour_requested", _types(out))

    def test_real_tour_slot_reply_still_detected(self):
        convo = [
            {"direction": "outbound", "content": "Can you tour Thursday at 2pm?"},
            {"direction": "inbound", "content": "Wednesday works for us."},
        ]
        self.assertTrue(_looks_like_tour_slot_reply(convo, "wednesday works for us."))

    def test_requested_tour_slot_declarative_confirmation_is_retained(self):
        message = "The requested tour slot works."
        convo = [
            {"direction": "outbound", "content": "Can you confirm the requested tour slot?"},
            {"direction": "inbound", "content": message},
        ]

        self.assertTrue(_looks_like_tour_slot_reply(convo, message.lower()))
        self.assertIn("tour_requested", _types(augment({"events": []}, convo)))

    def test_unrelated_clock_time_does_not_become_tour_slot_reply(self):
        convo = [
            {"direction": "outbound", "content": "Can you confirm a tour date and requested arrival time?"},
            {"direction": "inbound", "content": "I will send the rent schedule at 2 PM."},
        ]
        self.assertFalse(
            _looks_like_tour_slot_reply(convo, "i will send the rent schedule at 2 pm.")
        )
        out = augment({"events": []}, convo)
        self.assertNotIn("tour_requested", _types(out))

    def test_bare_time_remains_a_valid_reply_in_real_tour_context(self):
        convo = [
            {"direction": "outbound", "content": "Would Tuesday at 10 AM or 2 PM work for a tour?"},
            {"direction": "inbound", "content": "2 PM."},
        ]
        self.assertTrue(_looks_like_tour_slot_reply(convo, "2 pm."))

    def test_first_person_availability_remains_valid_in_real_tour_context(self):
        convo = [
            {"direction": "outbound", "content": "Would Tuesday at 10 AM or 2 PM work for a tour?"},
            {"direction": "inbound", "content": "I'm available at 2 PM."},
        ]
        self.assertTrue(_looks_like_tour_slot_reply(convo, "i'm available at 2 pm."))

    def test_non_tour_subjects_do_not_use_tour_reply_keywords(self):
        messages = (
            "The lease commencement date works for us Tuesday.",
            "I confirmed the rent schedule at 2 PM.",
            "I could do the rent schedule tomorrow.",
            "I am available at 2 PM for a pricing call.",
            "The rent schedule is attached. Tuesday works for us.",
            "The pricing call moved. I'm available at 2 PM.",
            "The lease terms are attached. Sounds good.",
            "I cannot show the financial model at that time.",
            "I can't show you the rent schedule Tuesday.",
            "I am not able to show you the lease terms Tuesday.",
            "The pricing meeting is confirmed for Tuesday at 2 PM.",
            "Our call is confirmed for Tuesday at 2 PM.",
            "I can't show you the floor plan until Tuesday.",
            "I cannot show the cash-flow projections Tuesday.",
            "I cannot show the floor plan Tuesday; could we do Wednesday?",
            "The pricing call moved Tuesday; could we do Wednesday?",
            "The pricing call moved. Could we do Wednesday at 2 PM instead?",
            "I reviewed the property tax model. I can show it Tuesday.",
            "The floor plan covers the property. I can show it Tuesday.",
            "This one is a property tax model. I can show it Tuesday.",
            "I reviewed the tenant vacating schedule. I can show it Tuesday.",
            "We discussed the tenant move out timeline. I can show it Tuesday.",
            "You are welcome to visit the property page Tuesday.",
            "I can let your client into the property model Tuesday.",
            "We can accommodate a visit to discuss pricing Tuesday.",
            "I can provide access to the floor plan Tuesday.",
            "The rent review at that time is confirmed.",
            "The pricing call at that slot works for us.",
            "The financial model at that time is unavailable.",
            "The floor plan at that slot is confirmed.",
            "The lease schedule at that time doesn't work.",
            "Rent is confirmed at that time.",
            "Pricing does not work at that slot.",
            "Model review is unavailable at that time.",
            "Floor plan is confirmed at that slot.",
            "Lease terms work for us at that time.",
            "The tour report at that time is confirmed.",
            "We reviewed when the tenant vacates. I can show it Tuesday.",
            "We discussed when the tenant moves out. I can show it Tuesday.",
            "The schedule notes when the tenant vacates. I can show it Tuesday.",
            "The timeline records when the tenant moves out. I can show it Tuesday.",
            "We discussed the tour schedule for when the tenant moves out. I can show it Tuesday.",
            "The tour timeline notes when the tenant vacates. I can show it Tuesday.",
            "The pricing call is at 2 PM. That time works.",
            "The rent review is scheduled for Tuesday. That time is confirmed.",
            "The pricing call is confirmed. That no longer works.",
            "The lease meeting is at 10 AM. That slot is unavailable.",
            "I can provide access to the tenant schedule after the tenant moves out. I can show it Tuesday.",
            "I can let them review the floor plan after the tenant moves out. I can show it Tuesday.",
            "I can visit the pricing model once the tenant vacates. I can show it Tuesday.",
            "I can show you the property Tuesday online.",
            "I can show you the property Tuesday in the financial model.",
            "Would your client like to see the property on the listing page?",
            "Let me know if your client wants to schedule a tour Tuesday via Zoom.",
            "Let me know if your client wants to schedule a tour Tuesday online.",
            "Let me know if your client wants to schedule a tour Tuesday in the financial model.",
            "You are welcome to visit the property Tuesday to review the lease.",
            "I can let your client into the property Tuesday for the pricing call.",
            "We can accommodate a visit Tuesday to discuss pricing.",
            "I can provide access at 2 PM to the floor plan.",
            "The tour report is available Tuesday.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. via Zoom.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. online.",
            "Let me know if your client wants to schedule a tour Tue., Aug. 11, 2026 at 2 p.m. in the financial model.",
        )
        for message in messages:
            with self.subTest(message=message):
                convo = [
                    {
                        "direction": "outbound",
                        "content": "Can you confirm a tour date and requested arrival time?",
                    },
                    {"direction": "inbound", "content": message},
                ]
                self.assertFalse(_looks_like_tour_slot_reply(convo, message.lower()))
                out = augment({"events": []}, convo)
                self.assertNotIn("tour_requested", _types(out))

    def test_day_only_and_timed_reschedules_are_tour_slot_replies(self):
        for message in (
            "I cannot show Tuesday; could we do Wednesday?",
            "I cannot show Tuesday; could we do Wednesday at 2 PM?",
            "The rent schedule is attached. That time no longer works; could we do Wednesday at 2 PM for the tour?",
        ):
            with self.subTest(message=message):
                convo = [
                    {
                        "direction": "outbound",
                        "content": "Can you confirm a tour date and requested arrival time?",
                    },
                    {"direction": "inbound", "content": message},
                ]
                self.assertTrue(_looks_like_tour_slot_reply(convo, message.lower()))
                out = augment({"events": []}, convo)
                tours = [event for event in out["events"] if event.get("type") == "tour_requested"]
                self.assertEqual(1, len(tours))
                self.assertEqual("tour_slot_reply", tours[0].get("reason"))

                repaired = augment({
                    "events": [{
                        "type": "tour_requested",
                        "reason": "tour_unavailable",
                        "question": message,
                    }],
                }, convo)
                repaired_tours = [
                    event for event in repaired["events"]
                    if event.get("type") == "tour_requested"
                ]
                self.assertEqual("tour_slot_reply", repaired_tours[0].get("reason"))


# ===========================================================================
# FIX-05 — repair a model-emitted tour_requested carrying a wrong reason (M18).
# ===========================================================================
class FIX05_TourReasonRepair(unittest.TestCase):
    def test_m18_wrong_reason_repaired_to_tour_unavailable(self):
        convo = [
            {"direction": "outbound", "content": "I'd like to schedule a tour. Does Thursday at 2pm work?"},
            {"direction": "inbound", "content": "No tours till further notice."},
        ]
        seed = [{"type": "tour_requested", "reason": "scheduling",
                 "notes": "Broker indicates tours are not available at this time"}]
        out = augment({"events": list(seed)}, convo)
        tours = [e for e in out["events"] if e.get("type") == "tour_requested"]
        self.assertTrue(tours)
        self.assertEqual(tours[0]["reason"], "tour_unavailable")


# ===========================================================================
# CodeRabbit PR#15 — alternate-viable must bind to the same subject; a separate
# available referral must NOT mask a terminal signal on the TARGET listing.
# ===========================================================================
class CodeRabbit_SplitProperty(unittest.TestCase):
    def test_target_terminal_survives_separate_available_referral(self):
        text = ("4501 Hollins Ferry Rd has been leased and is no longer available. "
                "Separately, we have another suite that is still available.")
        self.assertTrue(_fires_pu(text, target_anchor="4501 Hollins Ferry Rd, Baltimore"))


# ===========================================================================
# FIX-08 (+ FIX-09/10/11/12 prompt rules) — mechanical prompt-builder assertions.
# The stripped newest segment must reach the model as the AUTHORITATIVE last human
# message; prompt-level rules must be present. No live model call (client mocked).
# ===========================================================================
class FIX08_PromptBuilder(unittest.TestCase):
    QUOTED_TRAP = (
        "Checking in -- is your client still active in the market? I may have some "
        "new options coming this fall...\n\n"
        "On Wed, Jul 1, 2026 Marcus Reyes wrote:\n"
        "> That suite is no longer available.\n"
        "> We ended up leasing it to a 3PL group."
    )

    def _capture_prompt(self, conversation, contact_name=None):
        fake_response = mock.Mock()
        fake_response.output_text = '{"updates": [], "events": [], "response_email": null}'
        fake_response.usage = None
        fake_response.id = "resp_test"
        fake_client = mock.Mock()
        fake_client.responses.create.return_value = fake_response
        column_config = detect_column_mapping(
            ["Property Address", "Rent/SF /Yr"],
            use_ai=False,
        )
        column_config["customFields"] = {}
        with mock.patch.object(ai, "client", fake_client):
            ai.propose_sheet_updates(
                uid="u", client_id="c", email="dana@harborpointcre.com",
                sheet_id="s", header=["Property Address", "Rent/SF /Yr"], rownum=3,
                rowvals=["4501 Hollins Ferry Rd", ""], thread_id="t",
                conversation=conversation, contact_name=contact_name,
                column_config=column_config,
                extraction_fields=column_config["extractionFields"],
                dry_run=True,
            )
        call = fake_client.responses.create.call_args
        return call.kwargs["input"][0]["content"][-1]["text"]

    def test_stripped_newest_is_authoritative_last_human_message(self):
        convo = [{"direction": "inbound", "from": "dana@harborpointcre.com",
                  "content": self.QUOTED_TRAP}]
        prompt = self._capture_prompt(convo)
        # The authoritative block exists and carries ONLY the unquoted newest segment.
        self.assertIn("LAST HUMAN MESSAGE (AUTHORITATIVE", prompt)
        anchor = "LAST HUMAN MESSAGE (AUTHORITATIVE"
        auth_block = prompt[prompt.index(anchor): prompt.index("CONVERSATION HISTORY")]
        self.assertIn("Checking in", auth_block)
        self.assertNotIn("3PL group", auth_block)
        # The quoted rejection is still available as CONTEXT (full history), just not
        # as the authoritative last-human text.
        self.assertIn("3PL group", prompt)

    def test_fix09_referral_and_pdf_rules_present(self):
        convo = [{"direction": "inbound", "content": "Still available."}]
        prompt = self._capture_prompt(convo)
        self.assertIn("REFERRAL-TRIGGERED", prompt)
        self.assertIn("sourced only from a PDF", prompt)

    def test_fix10_subject_attribution_and_ooo_rules_present(self):
        convo = [{"direction": "inbound", "content": "Still available."}]
        prompt = self._capture_prompt(convo)
        self.assertIn("SUBJECT ATTRIBUTION", prompt)          # M26 CC third-party
        self.assertIn("TEMPORARY ABSENCE", prompt)            # M08 OOO
        self.assertIn("forward-then-introduce", prompt)       # M07 self-redirect

    def test_fix11_12_enum_and_confidential_scope_present(self):
        convo = [{"direction": "inbound", "content": "Still available."}]
        prompt = self._capture_prompt(convo)
        self.assertIn("never invent", prompt)                 # M22/M23 off-enum reasons
        self.assertIn("gate/visitor list", prompt)            # M14 confidential scope

    def test_tour_prompt_rejects_courtesy_and_requires_exact_evidence(self):
        prompt = self._capture_prompt([
            {"direction": "inbound", "content": "Please let me know if you need a tour."}
        ])
        self.assertIn("generic courtesy sign-off", prompt)
        self.assertIn("exact broker-authored sentence", prompt)
        self.assertIn("copied verbatim without paraphrasing", prompt)

    def test_fix13_neutral_greeting_when_mapped_name_disagrees(self):
        # M30: mapped contact 'Jordan Lee' but sender is Patricia Wong.
        convo = [{"direction": "inbound", "from": "pwong@keystoneindustrial.com",
                  "fromName": "Patricia Wong",
                  "content": "The space at 4501 Hollins Ferry is available - 42,000 SF."}]
        prompt = self._capture_prompt(convo, contact_name="Jordan Lee")
        self.assertNotIn("SUGGESTED GREETING NAME: Jordan", prompt)
        self.assertIn("greet NEUTRALLY", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
