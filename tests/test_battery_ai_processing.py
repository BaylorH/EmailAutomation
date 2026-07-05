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

    def test_r22_hypothetical_rent_keyword_anchored_returns_none(self):
        # Break R22: "Rent would have been $16/SF NNN if it were a fit." — the rent
        # keyword itself anchors the match, so "would have been" sits INSIDE the match
        # span (not before match.start()). Must still be read as hypothetical → None.
        text = "Rent would have been $16/SF NNN if it were a fit."
        self.assertIsNone(a._extract_rent_sf_yr_from_text(text))

    def test_r22_hypothetical_wouldve_contraction_returns_none(self):
        text = "Rent would've been $22 psf if the ceilings were taller."
        self.assertIsNone(a._extract_rent_sf_yr_from_text(text))

    def test_r22_hypothetical_leading_it_would_be_returns_none(self):
        # dollar-anchored variant (guard already handled this; regression lock)
        text = "It would be $16/SF NNN if it were a fit."
        self.assertIsNone(a._extract_rent_sf_yr_from_text(text))

    def test_r22_augment_does_not_apply_hypothetical_rent(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["Prop", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, cfg,
            _conv("Rent would have been $16/SF NNN if it were a fit."))
        self.assertIsNone(a._proposal_update_for_column(out, "Rent/SF /Yr"))

    def test_r22_control_current_rent_still_extracted(self):
        # guard must not over-suppress: a real current asking figure still extracts
        self.assertEqual(
            a._extract_rent_sf_yr_from_text("Rent is $16/SF NNN and it's available now."),
            "16.00")

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

    def test_r08_ti_allowance_is_not_base_rent(self):
        # A TI/tenant-improvement allowance is a landlord concession, never the
        # asking rent. The $/SF figure here must not be mined as Rent/SF/Yr.
        self.assertIsNone(
            a._extract_rent_sf_yr_from_text("We can offer $30/SF in TI allowance."))
        self.assertIsNone(
            a._extract_rent_sf_yr_from_text("TI allowance of $30/SF is available."))
        self.assertIsNone(
            a._extract_rent_sf_yr_from_text(
                "We can provide $25 PSF in tenant improvement allowance."))
        self.assertIsNone(
            a._extract_rent_sf_yr_from_text("Landlord will contribute $40/SF concession."))

    def test_r08_augment_writes_no_rent_for_ti_allowance(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["Prop", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, cfg,
            _conv("We can offer $30/SF in TI allowance."))
        self.assertIsNone(a._proposal_update_for_column(out, "Rent/SF /Yr"))

    def test_r13_opex_is_dollar_phrasing(self):
        # LIVE break: "keyword ... is $N" gap the _OPS_EX_RE didn't cover.
        text = "OpEx is $16/SF."
        self.assertEqual(a._extract_ops_ex_sf_from_text(text), "16.00")

    def test_r15_nnn_charges_are_dollar_phrasing(self):
        # LIVE break: "NNN charges are $N" parsed as OpEx, never as base rent.
        text = "NNN charges are $7.25/SF/yr."
        self.assertEqual(a._extract_ops_ex_sf_from_text(text), "7.25")
        self.assertIsNone(a._extract_rent_sf_yr_from_text(text))

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

    # R20 — recency/"now" preference: a current asking rate supersedes a stale
    # prior quote in the same line. First-match ordering returned the superseded
    # $22 quote; the deterministic guard must prefer the "now" figure.
    def test_r20_now_supersedes_stale_prior_quote(self):
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "We had quoted $22/SF but it is now $26/SF."), "26.00")

    def test_r20_current_asking_wins_over_earlier_figure(self):
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "We were at $22/SF NNN, current asking is $26/SF."), "26.00")

    def test_r20_now_monthly_is_annualized(self):
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "Was $1.00/SF/month, it's now $1.25/SF/month."), "15.00")

    def test_r20_no_recency_marker_keeps_first_match(self):
        # regression guard: without a recency marker the pre-existing first-match
        # contract is unchanged (do not over-apply the recency preference).
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "Asking $22/SF, comps in the area around $26/SF."), "22.00")

    # -- LIVE break R09: a TI allowance is a landlord give-back, not the asking rent.
    def test_ti_allowance_is_not_base_rent(self):
        self.assertIsNone(a._extract_rent_sf_yr_from_text(
            "Landlord provides a $25/SF tenant improvement allowance."))
        # abbreviation + leading-marker phrasings must be caught too.
        self.assertIsNone(a._extract_rent_sf_yr_from_text(
            "We can offer a $25/SF TI allowance on a 5-year term."))
        self.assertIsNone(a._extract_rent_sf_yr_from_text(
            "TI allowance of $25/SF is available with a qualified tenant."))

    def test_ti_allowance_augment_writes_no_rent(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["Prop", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, cfg,
            _conv("Landlord provides a $25/SF tenant improvement allowance."))
        self.assertIsNone(a._proposal_update_for_column(out, "Rent/SF /Yr"))

    # -- LIVE break R10: a free-rent concession is not the asking rate.
    def test_free_rent_concession_is_not_base_rent(self):
        self.assertIsNone(a._extract_rent_sf_yr_from_text(
            "Offering $5/SF free rent concession the first year."))
        self.assertIsNone(a._extract_rent_sf_yr_from_text(
            "We can include $5/SF in rent abatement over the term."))

    def test_free_rent_concession_augment_writes_no_rent(self):
        header = ["Property Address", "Rent/SF /Yr"]
        rowvals = ["Prop", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, rowvals, header, cfg,
            _conv("Offering $5/SF free rent concession the first year."))
        self.assertIsNone(a._proposal_update_for_column(out, "Rent/SF /Yr"))

    def test_real_asking_rent_still_extracted_despite_concession_mention(self):
        # The guard is match-local: a real asking rate alongside a TI allowance
        # must still be extracted (only the give-back figure is suppressed).
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "Asking $20/SF with a $25/SF TI allowance."), "20.00")
        self.assertEqual(a._extract_rent_sf_yr_from_text(
            "Base rent is $18/SF; we can offer $5/SF free rent the first year."), "18.00")

    # R17 — Canadian TMI (Taxes / Maintenance / Insurance) is the Canadian
    # equivalent of NNN/CAM operating expenses and must be recognized as OpEx.
    def test_r17_tmi_estimated_at_is_opex(self):
        # Exact live-testing break phrasing.
        self.assertEqual(
            a._extract_ops_ex_sf_from_text("TMI is estimated at $9.50 psf."),
            "9.50")

    def test_r17_tmi_phrasing_variants_are_opex(self):
        for text, expected in (
            ("TMI is $9.50 psf", "9.50"),
            ("TMI of $9.50/sf", "9.50"),
            ("TMI: $9.50 psf", "9.50"),
            ("TMI runs $9.50 per sf", "9.50"),
            ("$9.50 psf TMI on this space", "9.50"),
        ):
            with self.subTest(text=text):
                self.assertEqual(a._extract_ops_ex_sf_from_text(text), expected, text)

    def test_r17_tmi_augment_writes_ops_ex_column(self):
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
        rowvals = ["Toronto Bldg", "", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr", "ops_ex_sf": "Ops Ex /SF"}}
        out = a._augment_proposal_with_deterministic_extractions(
            {"updates": [], "events": []}, rowvals, header, cfg,
            _conv("TMI is estimated at $9.50 psf."))
        opex = a._proposal_update_for_column(out, "Ops Ex /SF")
        self.assertIsNotNone(opex)
        self.assertEqual(opex["value"], "9.50")

    def test_r17_tmi_mention_without_figure_is_not_mined(self):
        # A bare TMI mention with no attached $ figure must not fabricate an OpEx.
        self.assertIsNone(
            a._extract_ops_ex_sf_from_text("TMI is included in the quoted rate."))
        # A TMI label sitting far from a base-rent $ figure must not grab it as OpEx.
        self.assertIsNone(
            a._extract_ops_ex_sf_from_text(
                "TMI included; the base rent that we are asking is $24/sf."))


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

    # D04 — word-number dock count ("Four dock-high doors") must be KEPT.
    def test_d04_word_number_dock_count_kept(self):
        self.assertTrue(
            a._has_explicit_feature_count("Four dock-high doors.", a._DOCK_KW))
        proposal = {"updates": [{"column": "Docks", "value": "4"}], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("Four dock-high doors."), self.HEADER, self.CFG)
        self.assertIn("Docks", [u["column"] for u in out["updates"]])

    def test_d04_word_number_drive_in_count_kept(self):
        self.assertTrue(
            a._has_explicit_feature_count("Two grade-level drive-in doors.", a._DRIVE_IN_KW))
        proposal = {"updates": [{"column": "Drive Ins", "value": "2"}], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("Two grade-level drive-in doors."), self.HEADER, self.CFG)
        self.assertIn("Drive Ins", [u["column"] for u in out["updates"]])

    def test_d04_compound_word_number_dock_count_kept(self):
        self.assertTrue(
            a._has_explicit_feature_count("Twenty-four dock doors on site.", a._DOCK_KW))

    # Guard integrity: a spelled electrical spec must NOT read as a door count.
    def test_d04_three_phase_word_does_not_fabricate_dock(self):
        self.assertFalse(
            a._has_explicit_feature_count("Building has three-phase power.", a._DOCK_KW))
        proposal = {"updates": [{"column": "Docks", "value": "3"}], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("Building has three-phase power."), self.HEADER, self.CFG)
        self.assertNotIn("Docks", [u["column"] for u in out["updates"]])

    def test_d01_word_number_dock_count_is_kept(self):
        # Broker spelled the count as a word ("Two docks"), not a digit. The
        # guard must recognize word-numbers or it silently drops a real count.
        proposal = {"updates": [{"column": "Docks", "value": "2"},
                                 {"column": "Drive Ins", "value": "1"}], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("Two docks and one drive-in."), self.HEADER, self.CFG)
        cols = {u["column"] for u in out["updates"]}
        self.assertEqual(cols, {"Docks", "Drive Ins"})

    def test_d01_has_explicit_feature_count_word_number(self):
        self.assertTrue(a._has_explicit_feature_count("Two docks", a._DOCK_KW))
        self.assertTrue(a._has_explicit_feature_count("one drive-in", a._DRIVE_IN_KW))

    def test_d01_word_number_does_not_break_electrical_exclusion(self):
        # A qualitative loading phrase with no count still fabricates nothing.
        proposal = {"updates": [{"column": "Drive Ins", "value": "1"}], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("Property offers grade-level loading and three-phase power."),
            self.HEADER, self.CFG)
        cols = [u["column"] for u in out["updates"]]
        self.assertNotIn("Drive Ins", cols)


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
# Call-request escalation — a phone-call request must reach the operator, never
# auto-send (LIVE break: call_lets_hop). "Let's hop on a call" with no phone
# number intermittently drafts an auto-reply asking for a number/time instead of
# escalating. A deterministic guard nulls response_email whenever call_requested
# fires — whether or not a phone number is present — model-independently.
# ---------------------------------------------------------------------------
class CallRequestEscalationTests(unittest.TestCase):
    def test_call_lets_hop_no_phone_suppresses_autoreply(self):
        # The break: LLM fired call_requested but also drafted an auto-reply.
        types, proposal = _event_pipeline(
            [{"type": "call_requested"}],
            "Let's hop on a quick call tomorrow AM.")
        self.assertIn("call_requested", types)
        self.assertIsNone(proposal["response_email"],
                          "call_requested must escalate to operator, not auto-reply")

    def test_call_request_fires_deterministically_when_llm_misses(self):
        # Model-independence: even if the LLM emits no event, the fresh call
        # phrase must fire call_requested AND suppress the drafted auto-reply.
        types, proposal = _event_pipeline([], "Let's hop on a quick call tomorrow AM.")
        self.assertIn("call_requested", types)
        self.assertIsNone(proposal["response_email"])

    def test_call_me_at_number_still_escalates(self):
        # Phone number present — still escalate (no auto-send) per the break spec.
        types, proposal = _event_pipeline(
            [{"type": "call_requested"}],
            "Give me a call at 555-1212 to discuss 8200 Trade Center Dr.")
        self.assertIn("call_requested", types)
        self.assertIsNone(proposal["response_email"])

    def test_call_only_in_quote_does_not_refire_or_suppress(self):
        # Fresh text has no call ask — quoted history must not re-fire the event
        # nor null the response_email (no false-positive escalation).
        types, proposal = _event_pipeline(
            [],
            "Thanks Jill, flyer attached.\n"
            "> Please give me a call at 555-1212 to discuss 8200 Trade Center Dr.")
        self.assertNotIn("call_requested", types)
        self.assertEqual(proposal["response_email"], "auto-reply body")


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


# ---------------------------------------------------------------------------
# Quoted-tail detection edge cases — forwarded Outlook header (bare From:, no
# angle-bracket email) and Gmail/Apple attribution line whose "wrote" is NOT at
# line end. Both previously left quoted history glued to the fresh message, so a
# stale "leased / off the market" signal in the quote bled into a fresh reply and
# fired property_unavailable. Grounded in real Jill broker phrasing (Ryan Wilson /
# rwilson@ecrtx.com, 311 E Saint Elmo Rd; Pierce Demarco / 3520 Comsouth Dr).
# ---------------------------------------------------------------------------
class QuotedTailDetectionTests(unittest.TestCase):
    def _pipeline(self, events, body):
        proposal = {"events": [dict(e) for e in events], "updates": [],
                    "response_email": "auto-reply body"}
        proposal = a._suppress_quote_only_events(proposal, _conv(body))
        proposal = a._augment_events_with_deterministic_signals(proposal, _conv(body))
        return [e.get("type") for e in proposal["events"]]

    # H36 — forwarded Outlook header with a BARE From: (no <email>) starts the quote
    def test_h36_bare_outlook_forward_header_splits_quote(self):
        body = (
            "Still available - see the thread below for background.\n"
            "\n"
            "From: Ryan Wilson\n"
            "Sent: Monday, June 1, 2026 3:00 PM\n"
            "To: Jill Anderson\n"
            "Subject: RE: 311 E Saint Elmo Rd, Austin\n"
            "311 E Saint Elmo Rd is now fully leased and off the market.")
        fresh, quoted = a._split_fresh_and_quoted(body)
        self.assertIn("still available", fresh.lower())
        self.assertNotIn("fully leased", fresh.lower())
        self.assertIn("fully leased", quoted.lower())

    def test_h36_bare_outlook_forward_header_suppresses_unavailable(self):
        body = (
            "Still available - see the thread below for background.\n"
            "\n"
            "From: Ryan Wilson\n"
            "Sent: Monday, June 1, 2026 3:00 PM\n"
            "To: Jill Anderson\n"
            "Subject: RE: 311 E Saint Elmo Rd, Austin\n"
            "311 E Saint Elmo Rd is now fully leased and off the market.")
        types = self._pipeline([{"type": "property_unavailable", "reason": "leased"}], body)
        self.assertNotIn("property_unavailable", types,
                         f"leased signal lives only in quoted Outlook forward: {types}")

    # H37 — Gmail/Apple attribution whose "wrote" is NOT at line end starts the quote
    def test_h37_attribution_wrote_not_lineend_splits_quote(self):
        body = (
            "Yes it's available, actively marketing.\n"
            "On Jun 1, 2026 at 3:00 PM Pierce Demarco wrote the following:\n"
            "3520 Comsouth Dr is fully leased, no longer on the market.")
        fresh, quoted = a._split_fresh_and_quoted(body)
        self.assertIn("actively marketing", fresh.lower())
        self.assertNotIn("fully leased", fresh.lower())
        self.assertIn("no longer on the market", quoted.lower())

    def test_h37_attribution_wrote_not_lineend_suppresses_unavailable(self):
        body = (
            "Yes it's available, actively marketing.\n"
            "On Jun 1, 2026 at 3:00 PM Pierce Demarco wrote the following:\n"
            "3520 Comsouth Dr is fully leased, no longer on the market.")
        types = self._pipeline([{"type": "property_unavailable", "reason": "leased"}], body)
        self.assertNotIn("property_unavailable", types,
                         f"leased signal lives only in quoted attribution tail: {types}")

    # controls — do NOT over-split fresh prose that merely resembles a marker
    def test_control_prose_from_line_not_split(self):
        # A single "From:" style prose line with no Outlook block must stay fresh.
        fresh, quoted = a._split_fresh_and_quoted(
            "From: my perspective the space is still available and marketing.")
        self.assertEqual(quoted, "")

    def test_control_on_wrote_without_date_not_split(self):
        # "On ... wrote" with no date/time token is casual prose, not an attribution.
        fresh, quoted = a._split_fresh_and_quoted(
            "On our recent call I wrote up the numbers; still available.")
        self.assertEqual(quoted, "")

    def test_control_strict_wrote_lineend_still_splits(self):
        fresh, quoted = a._split_fresh_and_quoted(
            "Sounds good.\nOn Mon, Jun 1, 2026 at 3:00 PM Pierce Demarco <pierce.demarco@freehillco.com> wrote:\n> old text")
        self.assertIn("sounds good", fresh.lower())
        self.assertIn("old text", quoted.lower())


if __name__ == "__main__":
    unittest.main()
