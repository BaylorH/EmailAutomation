"""Deterministic battery for classifier/extraction breaks found via LIVE testing.

Each test drives a real guard/extraction function in email_automation.ai_processing
directly (no live OpenAI), so behavior is model-independent. Grounded in real Jill
broker phrasing (Wilson/Clark/DeMarco rent lines, quoted-thread replies).

Break IDs map to the live-testing report:
  R13/X03/R05/R09/R04/S03/D07 — extraction; E_*/F_*/H_* — quoted-history events;
  B20/L21/M22 — link surfacing + prompt truncation.
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

from email_automation import ai_processing as a  # noqa: E402


def _conv(body, direction="inbound"):
    return [{"direction": direction, "from": "mark@cbre.com", "to": ["jill@x.com"],
             "subject": "Re: 8200 Trade Center Dr", "timestamp": "2026-07-05T00:00:00Z",
             "content": body}]


def _event_pipeline(events, body):
    """Mirror production order: suppress quote-only events, then augment."""
    proposal = {"events": [dict(e) for e in events], "updates": [], "response_email": "auto-reply body"}
    proposal = a._suppress_quote_only_events(proposal, _conv(body))
    proposal = a._augment_events_with_deterministic_signals(proposal, _conv(body))
    return [e.get("type") for e in proposal["events"]], proposal


# ---------------------------------------------------------------------------
# Extraction breaks (deterministic fallback)
# ---------------------------------------------------------------------------
class RentOpexSfExtractionTests(unittest.TestCase):
    def test_r13_rent_plus_opex_inline_rent_is_base_not_opex(self):
        text = "We can do $24 + $8/sf opex on the larger spaces."
        self.assertEqual(a._extract_rent_sf_yr_from_text(text), "24.00")
        self.assertEqual(a._extract_ops_ex_sf_from_text(text), "8.00")

    def test_r13_augment_writes_rent_24_not_opex_8(self):
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
        rowvals = ["Wilson Bldg", "", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr", "ops_ex_sf": "Ops Ex /SF"}}
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, cfg,
            _conv("We can do $24 + $8/sf opex on the larger spaces."))
        rent = a._proposal_update_for_column(out, "Rent/SF /Yr")
        opex = a._proposal_update_for_column(out, "Ops Ex /SF")
        self.assertIsNotNone(rent)
        self.assertEqual(rent["value"], "24.00")
        self.assertNotEqual(rent["value"], "8.00")
        self.assertEqual(opex["value"], "8.00")

    def test_x03_nonviable_hypothetical_writes_no_rent(self):
        text = ("Honestly this space is more office-heavy than warehouse and has no "
                "drive-in, probably not a fit. Rent would have been $16/SF NNN.")
        self.assertIsNone(a._extract_rent_sf_yr_from_text(text))

    def test_x03_augment_does_not_apply_16(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["Prop", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, cfg,
            _conv("Honestly this space is more office-heavy than warehouse and has no "
                  "drive-in, probably not a fit. Rent would have been $16/SF NNN."))
        self.assertIsNone(a._proposal_update_for_column(out, "Rent/SF /Yr"))

    def test_r05_combined_psf_month_base_rent_annualized(self):
        text = "2,000 SF: $1.25 NNN + $0.34 OPEX = $1.59 PSF / Month. Move in ready."
        self.assertEqual(a._extract_rent_sf_yr_from_text(text), "15.00")
        self.assertEqual(a._extract_ops_ex_sf_from_text(text), "4.08")
        self.assertEqual(a._extract_total_sf_from_text(text), "2000")

    def test_r09_ti_credit_rent_range_low_end(self):
        text = ("Quoted rates are between $20.00 - $22.00, depending on term credit and "
                "additional TI needs + $6.00 in opex.")
        rent = a._extract_rent_sf_yr_from_text(text)
        self.assertIsNotNone(rent)
        self.assertTrue(20.0 <= float(rent) <= 22.0, rent)
        self.assertEqual(a._extract_ops_ex_sf_from_text(text), "6.00")

    def test_r04_psf_abbreviation(self):
        self.assertEqual(
            a._extract_rent_sf_yr_from_text("We're quoting $12 psf NNN on this one."),
            "12.00")

    def test_s03_approx_sf_prefix(self):
        self.assertEqual(
            a._extract_total_sf_from_text("+/- 9,000 SF new free-standing building."),
            "9000")

    def test_s03_augment_writes_total_sf(self):
        header = ["Property Address", "Total SF"]
        rowvals = ["Prop", ""]
        cfg = {"mappings": {"total_sf": "Total SF"}}
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, cfg, _conv("+/- 9,000 SF new free-standing building."))
        upd = a._proposal_update_for_column(out, "Total SF")
        self.assertIsNotNone(upd)
        self.assertEqual(upd["value"], "9000")

    def test_existing_asking_rent_behavior_preserved(self):
        # regression guard for the pre-existing deterministic fallback contract
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "Asking $9.00/SF/year, NNN $0.39/SF, power is 200 amps."), "9.00")
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "Asking rate: $1.25/SF/month NNN."), "15.00")
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "Asking rent: $9.00/SF NNN, available next month."), "9.00")


class FabricatedDoorCountTests(unittest.TestCase):
    HEADER = ["Property Address", "Drive Ins", "Docks", "Power"]
    CFG = {"mappings": {"drive_ins": "Drive Ins", "docks": "Docks"}}

    def test_d07_grade_level_loading_does_not_fabricate_drive_in(self):
        proposal = {"updates": [{"column": "Drive Ins", "value": "1"},
                                {"column": "Power", "value": "3-phase"}], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("Property offers grade-level loading and 3-phase power."),
            self.HEADER, self.CFG)
        cols = [u["column"] for u in out["updates"]]
        self.assertNotIn("Drive Ins", cols)
        self.assertIn("Power", cols)  # power must be preserved

    def test_d07_explicit_count_is_kept(self):
        proposal = {"updates": [{"column": "Drive Ins", "value": "2"},
                                {"column": "Docks", "value": "3"}], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("Has 2 drive-in doors and 3 dock doors."), self.HEADER, self.CFG)
        cols = {u["column"] for u in out["updates"]}
        self.assertEqual(cols, {"Drive Ins", "Docks"})


# ---------------------------------------------------------------------------
# Quoted-history event breaks — the trigger phrase lives ONLY in quoted text
# ---------------------------------------------------------------------------
class QuotedHistoryEventTests(unittest.TestCase):
    def _assert_suppressed(self, etype, events, body):
        types, _ = _event_pipeline(events, body)
        self.assertNotIn(etype, types, f"{etype} must not fire off quoted history: {types}")

    def test_e_unavailable_only_in_quote(self):
        self._assert_suppressed("property_unavailable",
            [{"type": "property_unavailable", "reason": "x"}],
            "see attached updated flyer for 8200 Trade Center Dr\n"
            "> 8200 Trade Center Dr is no longer available.")

    def test_f_stale_unavail_fresh_still_available(self):
        self._assert_suppressed("property_unavailable",
            [{"type": "property_unavailable"}],
            "good news, 8200 Trade Center Dr is STILL AVAILABLE, prior deal fell through, flyer attached.\n"
            "> 8200 Trade Center Dr has been leased.")

    def test_f_stale_unavail_fresh_back_on_market(self):
        self._assert_suppressed("property_unavailable",
            [{"type": "property_unavailable"}],
            "Update: 8200 Trade Center is back on the market as of today. Available immediately.\n"
            ">>> Sorry, 8200 Trade Center leased. No longer available.")

    def test_h_other_property_unavailable_in_quote(self):
        self._assert_suppressed("property_unavailable",
            [{"type": "property_unavailable"}],
            "8200 Trade Center Dr is available and I've attached the flyer.\n"
            "> 500 Industrial Pkwy is no longer available.")

    def test_h_wrong_contact_only_in_quote(self):
        types, proposal = _event_pipeline(
            [{"type": "wrong_contact", "suggestedEmail": "dlee@cbre.com"}],
            "Here's the info you asked for on 8200 Trade Center Dr. - Mark\n"
            "> I no longer handle this property, please contact Dana Lee at dlee@cbre.com")
        self.assertNotIn("wrong_contact", types)

    def test_e_tour_only_in_quote(self):
        self._assert_suppressed("tour_requested",
            [{"type": "tour_requested"}],
            "specs attached for 8200 Trade Center Dr. Total SF 25,000.\n"
            "> Would you like to schedule a tour next week? Happy to show you around.")

    def test_e_optout_only_in_quote(self):
        self._assert_suppressed("contact_optout",
            [{"type": "contact_optout", "reason": "unsubscribe"}],
            "Sure, here's the flyer for 8200 Trade Center Dr.\n"
            "> Please remove me from your list, we do not work with tenant reps.")

    def test_e_call_only_in_quote(self):
        self._assert_suppressed("call_requested",
            [{"type": "call_requested"}],
            "Thanks Jill, flyer attached.\n"
            "> Please give me a call at 555-1212 to discuss 8200 Trade Center Dr.")

    def test_e_newproperty_only_in_quote(self):
        self._assert_suppressed("new_property",
            [{"type": "new_property", "address": "500 Industrial Pkwy"}],
            "Here's the flyer for 8200 Trade Center Dr as requested.\n"
            "> We also have 500 Industrial Pkwy available if you're interested.")

    def test_h_close_only_in_quote(self):
        self._assert_suppressed("close_conversation",
            [{"type": "close_conversation", "notes": "exclusive_with_another"}],
            "Sure, attaching the flyer for 8200 Trade Center Dr now.\n"
            "> We are going exclusive with another tenant rep, so let us close this out.")

    def test_h_property_issue_only_in_quote(self):
        self._assert_suppressed("property_issue",
            [{"type": "property_issue", "issue": "roof"}],
            "Flyer attached for 8200 Trade Center Dr.\n"
            "> Note the roof has significant damage and there is an environmental Phase II open.")

    def test_h_fwd_colleague_specs_no_wrong_contact(self):
        types, _ = _event_pipeline(
            [{"type": "wrong_contact", "suggestedEmail": "dlee@cbre.com"}],
            "forwarding my colleague's specs below.\n"
            "From: Dana Lee <dlee@cbre.com>\n8200 Trade Center Dr: 25,000 SF, $10.50")
        self.assertNotIn("wrong_contact", types)

    # controls — fresh signals MUST still fire / be kept
    def test_control_fresh_unavailable_still_fires(self):
        types, _ = _event_pipeline(
            [{"type": "property_unavailable"}],
            "8200 Trade Center Dr is no longer available.\n> older quoted thread text")
        self.assertIn("property_unavailable", types)

    def test_control_fresh_tour_kept(self):
        types, _ = _event_pipeline(
            [{"type": "tour_requested"}], "Happy to show you the space, when works for you?")
        self.assertIn("tour_requested", types)

    def test_control_no_quote_no_suppression(self):
        proposal = {"events": [{"type": "property_issue", "issue": "odor"}], "updates": []}
        out = a._suppress_quote_only_events(proposal, _conv("The building has a bad odor problem."))
        self.assertIn("property_issue", [e["type"] for e in out["events"]])


# ---------------------------------------------------------------------------
# Link surfacing + prompt truncation
# ---------------------------------------------------------------------------
class FlyerLinkAndTruncationTests(unittest.TestCase):
    def test_b20_broken_flyer_link_surfaced_in_notes(self):
        proposal = {"updates": [], "events": [], "notes": ""}
        url_texts = [{"url": "https://we.tl/t-expired99",
                      "text": "This transfer has expired and is no longer available."}]
        out = a._augment_proposal_with_flyer_link(
            proposal, url_texts, ["Prop", ""], ["Property Address", "Total SF"], {"mappings": {}})
        self.assertIn("we.tl/t-expired99", out.get("notes") or "")

    def test_b20_broken_flyer_link_surfaced_in_column(self):
        proposal = {"updates": [], "events": []}
        url_texts = [{"url": "https://we.tl/t-expired99",
                      "text": "This transfer has expired and is no longer available."}]
        out = a._augment_proposal_with_flyer_link(
            proposal, url_texts, ["Prop", ""], ["Property Address", "Flyer / Link"],
            {"mappings": {"flyer_link": "Flyer / Link"}})
        upd = a._proposal_update_for_column(out, "Flyer / Link")
        self.assertIsNotNone(upd)
        self.assertEqual(upd["value"], "https://we.tl/t-expired99")

    def test_b20_working_link_not_flagged(self):
        proposal = {"updates": [], "events": [], "notes": ""}
        url_texts = [{"url": "https://good.com/flyer.pdf", "text": "912 Gemini Dr - 40,000 SF flyer"}]
        out = a._augment_proposal_with_flyer_link(
            proposal, url_texts, ["Prop", ""], ["Property Address", "Total SF"], {"mappings": {}})
        self.assertNotIn("good.com", out.get("notes") or "")

    def test_l21_url_content_number_beyond_1000_reaches_model(self):
        text = ("x " * 700) + "Total SF: 25,000 SF" + (" y" * 700)  # number past char 1000
        clipped = a._clip_for_prompt(text, a._URL_TEXT_CHAR_LIMIT)
        self.assertIn("25,000 SF", clipped)

    def test_m22_pdf_content_number_beyond_8000_reaches_model(self):
        text = ("filler " * 1400) + "\nTotal SF: 25,000 SF\n" + ("tail " * 1400)  # past char 8000
        clipped = a._clip_for_prompt(text, a._PDF_TEXT_CHAR_LIMIT)
        self.assertIn("25,000 SF", clipped)

    def test_clip_retains_field_line_beyond_hard_cap(self):
        text = ("a" * 9000) + "\nTotal SF: 25,000 SF\n" + ("b" * 9000)
        clipped = a._clip_for_prompt(text, 8000)
        self.assertIn("25,000 SF", clipped)


if __name__ == "__main__":
    unittest.main()
