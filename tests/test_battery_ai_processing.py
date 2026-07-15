"""Deterministic battery for classifier/extraction breaks found via LIVE testing.

Each test drives a real guard/extraction function in email_automation.ai_processing
directly (no live OpenAI), so behavior is model-independent. Grounded in real Jill
broker phrasing (Wilson/Clark/DeMarco rent lines, quoted-thread replies).

Break IDs map to the live-testing report:
  R13/X03/R05/R09/R04/S03/D07 — extraction; E_*/F_*/H_* — quoted-history events;
  B20/L21/M22 — link surfacing + prompt truncation.
"""
import json
import os
import sys
import unittest
from unittest import mock

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

    def test_r22_hypothetical_combined_rent_opex_writes_neither(self):
        # Combined "base + opex" branch (branch 1) must honour the hypothetical
        # guard on BOTH the rent and opex extractors — a past-tense "would have
        # been $24 + $8 opex" is not a current figure.
        text = "Rent would have been $24 + $8/sf opex if we'd caught it earlier."
        self.assertIsNone(a._extract_rent_sf_yr_from_text(text))
        self.assertIsNone(a._extract_ops_ex_sf_from_text(text))

    def test_r22_hypothetical_range_returns_none(self):
        # Range branch (branch 2) must honour the hypothetical guard too.
        text = "Asking rates would have been between $20.00 - $22.00/SF if it fit."
        self.assertIsNone(a._extract_rent_sf_yr_from_text(text))

    def test_r22_hypothetical_standalone_opex_returns_none(self):
        text = "OpEx would have been $8/SF but the deal fell through."
        self.assertIsNone(a._extract_ops_ex_sf_from_text(text))

    def test_range_non_hypothetical_still_extracts_low_end(self):
        # Regression: a real (non-hypothetical) range must still yield the low end.
        text = "Asking rents are between $20.00 - $22.00/SF NNN."
        self.assertEqual(a._extract_rent_sf_yr_from_text(text), "20.00")

    # --- LIVE break 900 Alt Suggest St: cross-property fallback write --------
    def _night_hdr_cfg(self):
        header = ["Property Address", "Total SF", "Rent/SF /Yr", "Ops Ex / SF",
                  "Drive Ins", "Loading Docks"]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr", "ops_ex_sf": "Ops Ex / SF",
                            "total_sf": "Total SF"}}
        return header, cfg

    def test_augmenter_skips_specs_when_new_property_event(self):
        # "900 under LOI ... but 1100 Fresh Listing Ave is 30,000 SF at $10.50"
        # — the specs belong to the ALTERNATE; nothing may land on the 900 row.
        header, cfg = self._night_hdr_cfg()
        proposal = {"updates": [], "events": [
            {"type": "property_unavailable", "reason": "under_loi"},
            {"type": "new_property", "address": "1100 Fresh Listing Ave", "city": "Austin"},
        ]}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, ["900 Alt Suggest St", "", "", "", "", ""], header, cfg,
            _conv("900 Alt Suggest St went under LOI last week, so it's off the market. "
                  "But I just listed 1100 Fresh Listing Ave - 30,000 SF at $10.50/SF NNN."))
        self.assertEqual(out["updates"], [],
                         "fallback must not write the alternate property's specs to the dying row")

    def test_augmenter_skips_specs_when_property_unavailable_alone(self):
        header, cfg = self._night_hdr_cfg()
        proposal = {"updates": [], "events": [{"type": "property_unavailable", "reason": "leased"}]}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, ["Prop", "", "", "", "", ""], header, cfg,
            _conv("It's leased. It was going for $18/SF on 12,000 SF."))
        self.assertEqual(out["updates"], [])

    def test_attachment_regex_does_not_create_loading_updates_without_model(self):
        header, cfg = self._night_hdr_cfg()
        proposal = {"updates": [], "events": []}
        flyer = ("FOR LEASE - 600 Flyer Facts Blvd\n"
                 "Loading: 2 dock-high doors, 1 drive-in ramp\nPower: 600A, 3-phase")
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, ["600 Flyer Facts Blvd", "", "", "", "", ""], header, cfg,
            _conv("All the specs are in the attached flyer."),
            extra_texts=[flyer])
        di = a._proposal_update_for_column(out, "Drive Ins")
        dk = a._proposal_update_for_column(out, "Loading Docks")
        self.assertIsNone(di)
        self.assertIsNone(dk)

    def _run_loading_precedence_replay(
        self, broker_body, flyer_text, model_updates, docks_header="Docks",
    ) -> tuple[dict, str]:
        fake_response = mock.Mock()
        fake_response.output_text = json.dumps({
            "updates": model_updates,
            "events": [],
            "response_email": None,
            "notes": "",
        })
        fake_response.usage = None
        fake_response.id = "resp-loading-precedence-replay"
        fake_client = mock.Mock()
        fake_client.responses.create.return_value = fake_response
        with mock.patch.object(a, "client", fake_client):
            proposal = a.propose_sheet_updates(
                uid="baylor-proof",
                client_id="loading-precedence-replay",
                email="bp21harrison@gmail.com",
                sheet_id="proof-sheet",
                header=["Property Address", "Drive Ins", docks_header],
                rownum=3,
                rowvals=["570 W Cheyenne Ave", "", ""],
                thread_id="proof-thread",
                pdf_manifest=[{
                    "name": "older-flyer.pdf",
                    "text": flyer_text,
                    "method": "production-replay",
                }],
                conversation=_conv(broker_body),
                column_config={
                    "mappings": {"drive_ins": "Drive Ins", "docks": docks_header},
                    "extractionFields": ["drive_ins", "docks"],
                    "requiredFields": [],
                    "formulaFields": [],
                    "neverRequest": [],
                    "customFields": {},
                },
                dry_run=True,
            )
        request_content = fake_client.responses.create.call_args.kwargs["input"][0]["content"]
        prompt = request_content[-1]["text"]
        return proposal, prompt

    def test_proposal_latest_broker_counts_override_conflicting_flyer(self):
        broker_body = "The current setup has 4 dock-high doors and 1 drive-in."
        flyer_text = "OLDER FLYER: 1 dock-high door and 13 drive-ins."
        out, prompt = self._run_loading_precedence_replay(
            broker_body,
            flyer_text,
            [
                {"column": "Loading Docks", "value": "4", "confidence": 0.98,
                 "reason": "Latest broker message."},
                {"column": "Drive Ins", "value": "1", "confidence": 0.98,
                 "reason": "Latest broker message."},
            ],
            docks_header="Loading Docks",
        )
        self.assertNotIn("Trust ATTACHMENTS (PDFs) over the email body when numbers conflict.", prompt)
        self.assertIn(
            "FIELD VALUES ONLY: when the latest broker message and an attachment conflict, use the latest broker message.",
            prompt,
        )
        self.assertIn(broker_body, prompt)
        self.assertIn(flyer_text, prompt)
        self.assertEqual(a._proposal_update_for_column(out, "Loading Docks")["value"], "4")
        self.assertEqual(a._proposal_update_for_column(out, "Drive Ins")["value"], "1")

    def test_proposal_uses_flyer_counts_when_latest_broker_omits_them(self):
        broker_body = "All current loading details are in the attached flyer."
        flyer_text = "CURRENT FLYER: 2 dock-high doors and 1 drive-in."
        out, prompt = self._run_loading_precedence_replay(
            broker_body,
            flyer_text,
            [
                {"column": "Docks", "value": "2", "confidence": 0.96,
                 "reason": "Matched current property flyer."},
                {"column": "Drive Ins", "value": "1", "confidence": 0.96,
                 "reason": "Matched current property flyer."},
            ],
        )
        self.assertIn(
            "Use attachments only to fill field values that the latest broker message does not provide.",
            prompt,
        )
        self.assertIn(broker_body, prompt)
        self.assertIn(flyer_text, prompt)
        self.assertEqual(a._proposal_update_for_column(out, "Docks")["value"], "2")
        self.assertEqual(a._proposal_update_for_column(out, "Drive Ins")["value"], "1")

    def test_semantic_model_loading_updates_survive_deterministic_augmenter(self):
        header, cfg = self._night_hdr_cfg()
        proposal = {
            "updates": [
                {"column": "Loading Docks", "value": "4", "reason": "semantic model"},
                {"column": "Drive Ins", "value": "1", "reason": "semantic model"},
            ],
            "events": [],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["570 W Cheyenne Ave", "", "", "", "", ""],
            header,
            cfg,
            _conv("It has 4 docks and 1 drive-in."),
            extra_texts=["The old flyer lists 1 dock and 13 drive-ins."],
        )
        self.assertEqual(a._proposal_update_for_column(out, "Loading Docks")["value"], "4")
        self.assertEqual(a._proposal_update_for_column(out, "Drive Ins")["value"], "1")

    def test_loading_regex_never_creates_or_vetoes_semantic_updates(self):
        header, cfg = self._night_hdr_cfg()
        cases = [
            "The building has 2 docks, but the tenant requires 4 docks.",
            "Suite 100 has 2 docks. Suite 200 has 4 docks.",
            "The subject has 2 docks; the adjacent building has 6 docks.",
            "It could add 4 dock-high doors if the tenant signs.",
            "There is potential for 3 drive-ins after conversion.",
            "Door dimensions are 10 x 12 dock doors.",
            "There are 2 docks at the front and 3 docks at the rear.",
        ]
        for body in cases:
            with self.subTest(body=body):
                empty = a._augment_proposal_with_deterministic_extractions(
                    {"updates": [], "events": []},
                    ["570 W Cheyenne Ave", "", "", "", "", ""],
                    header,
                    cfg,
                    _conv(body),
                )
                self.assertIsNone(a._proposal_update_for_column(empty, "Loading Docks"))
                self.assertIsNone(a._proposal_update_for_column(empty, "Drive Ins"))

        semantic = a._augment_proposal_with_deterministic_extractions(
            {
                "updates": [
                    {"column": "Loading Docks", "value": "2", "reason": "semantic model"},
                ],
                "events": [],
            },
            ["570 W Cheyenne Ave", "", "", "", "", ""],
            header,
            cfg,
            _conv("The building has 2 docks, but the tenant requires 4 docks."),
        )
        self.assertEqual(
            a._proposal_update_for_column(semantic, "Loading Docks")["value"],
            "2",
        )

    def test_augmenter_never_guesses_counts_without_numbers(self):
        header, cfg = self._night_hdr_cfg()
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal, ["Prop", "", "", "", "", ""], header, cfg,
            _conv("The space has grade-level loading and dock access."),
            extra_texts=["Ample dock-high loading available."])
        self.assertIsNone(a._proposal_update_for_column(out, "Drive Ins"))
        self.assertIsNone(a._proposal_update_for_column(out, "Loading Docks"))

    def test_pdf_sourced_drive_in_count_survives_fabricated_guard(self):
        # The guard validated against the EMAIL text only, so a count stated
        # only in the flyer PDF was stripped as "fabricated". Flyer text is
        # legitimate evidence.
        header, cfg = self._night_hdr_cfg()
        proposal = {"updates": [
            {"column": "Drive Ins", "value": "1", "confidence": 0.9, "reason": "PDF flyer"},
        ], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("All the specs are in the attached flyer."), header, cfg,
            extra_texts=["Loading: 2 dock-high doors, 1 drive-in ramp"])
        self.assertIsNotNone(a._proposal_update_for_column(out, "Drive Ins"),
                             "flyer-sourced count must survive the fabricated-count guard")

    def test_fabricated_count_still_dropped_with_loading_docks_header(self):
        # "Loading Docks" header was previously unguarded (lookup only tried
        # "Docks"), letting invented counts through on Jill's real header.
        header, cfg = self._night_hdr_cfg()
        proposal = {"updates": [
            {"column": "Loading Docks", "value": "4", "confidence": 0.9, "reason": "?"},
        ], "events": []}
        out = a._suppress_fabricated_door_counts(
            proposal, _conv("The space has dock access."), header, cfg,
            extra_texts=["Grade-level loading available."])
        self.assertIsNone(a._proposal_update_for_column(out, "Loading Docks"),
                          "invented dock count must be dropped even with a 'Loading Docks' header")

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

    def test_multi_suite_explicit_total_overrides_individual_suite_areas(self):
        text = (
            "Suite A is 5,200 SF and Suite C is 4,800 SF. "
            "Together they provide 10,000 SF total."
        )

        self.assertEqual(a._extract_total_sf_from_text(text), "10000")

    def test_single_suite_area_behavior_is_preserved(self):
        self.assertEqual(
            a._extract_total_sf_from_text("Suite C is 4,800 SF and is ready now."),
            "4800",
        )

    def test_office_component_area_is_not_total_sf(self):
        component_phrases = (
            "The property has about 2000 sf of office.",
            "Office area is 2,000 SF.",
            "Total office area is 2,000 SF.",
            "The office portion is 2,000 SF.",
            "There is 2,000 SF dedicated to office.",
            "Office comprises 2,000 SF.",
            "About 2,000 SF is office.",
            "The warehouse portion: 20,000 SF.",
        )
        for phrase in component_phrases:
            with self.subTest(phrase=phrase):
                self.assertIsNone(a._extract_total_sf_from_text(phrase))

    def test_office_component_area_is_removed_from_model_total_sf_proposal(self):
        for proposed_value in ("2000", "2000.0", "2,000 SF", "2K SF"):
            with self.subTest(proposed_value=proposed_value):
                proposal = {
                    "updates": [{
                        "column": "Total SF",
                        "value": proposed_value,
                        "confidence": 0.90,
                        "reason": "The message states about 2,000 SF of office.",
                    }],
                    "events": [],
                }

                out = a._augment_proposal_with_deterministic_extractions(
                    proposal,
                    ["123 Test Dr", ""],
                    ["Property Address", "Total SF"],
                    {"mappings": {"total_sf": "Total SF"}},
                    _conv("The property has about 2,000 SF of office."),
                )

                self.assertIsNone(a._proposal_update_for_column(out, "Total SF"))

    def test_unrelated_attachment_rent_does_not_update_target_property(self):
        header = ["Property Address", "City", "Rent/SF /Yr"]
        rowvals = ["123 Test Dr", "Boise", ""]
        cfg = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}
        for proposed_value in (
            "15.00", "15", "15.0", "$15.00", "15.00/SF",
            "$15.00/SF (36 months)", "15.00/SF, 3-year term", "15.00-15.50",
            "3-year term at $15.00/SF",
        ):
            with self.subTest(proposed_value=proposed_value):
                proposal = {
                    "updates": [{
                        "column": "Rent/SF /Yr",
                        "value": proposed_value,
                        "confidence": 0.88,
                        "reason": "The attached brochure lists $15.00/SF/yr.",
                    }],
                    "events": [],
                }

                out = a._augment_proposal_with_deterministic_extractions(
                    proposal,
                    rowvals,
                    header,
                    cfg,
                    _conv("The 123 Test Dr property is available. The attached flyer has the details."),
                    pdf_manifest=[{
                        "name": "Spring Hill Exec Park - Marketing_Brochure_2025.pdf",
                        "text": (
                            "322 Spring Hill Dr, Spring, TX 77386. "
                            "LEASE RATE: $15.00 SF/yr (NNN)."
                        ),
                    }],
                )

                self.assertIsNone(a._proposal_update_for_column(out, "Rent/SF /Yr"))

    def test_term_first_target_rent_is_not_confused_with_competing_pdf_rent(self):
        proposal = {
            "updates": [{
                "column": "Rent/SF /Yr",
                "value": "3-year term at $15/SF",
                "confidence": 0.9,
            }],
            "events": [],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Test Dr", "Boise", ""],
            ["Property Address", "City", "Rent/SF /Yr"],
            {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}},
            _conv("Asking rent for 123 Test Dr is $15/SF for a 3-year term."),
            pdf_manifest=[{
                "name": "322 Spring Hill Dr flyer.pdf",
                "text": "322 Spring Hill Dr is asking $3/SF.",
            }],
        )
        self.assertEqual("15.00", a._proposal_update_for_column(out, "Rent/SF /Yr")["value"])

    def test_mixed_property_attachment_cannot_supply_target_rent(self):
        source = (
            "Portfolio overview for 123 Test Dr. "
            "322 Spring Hill Dr is asking $15.00/SF/yr."
        )
        self.assertFalse(
            a._attachment_can_supply_target_rent(
                source,
                "123 Test Dr, Boise",
                "The 123 Test Dr property is available; see attached.",
            )
        )

    def test_mixed_property_attachment_cannot_preserve_competing_total_sf(self):
        proposal = {
            "updates": [{
                "column": "Total SF",
                "value": "45,000 SF",
                "confidence": 0.9,
            }],
            "events": [{"type": "property_unavailable"}],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Test Dr", ""],
            ["Property Address", "Total SF"],
            {"mappings": {"total_sf": "Total SF"}},
            _conv("The 123 Test Dr property is unavailable; see attached."),
            pdf_manifest=[{
                "name": "portfolio flyer.pdf",
                "text": (
                    "Portfolio overview for 123 Test Dr. "
                    "322 Spring Hill Dr - Total Building Size: 45,000 SF."
                ),
            }],
        )

        self.assertIsNone(a._proposal_update_for_column(out, "Total SF"))

    def test_fact_suppression_runs_before_terminal_event_early_return(self):
        proposal = {
            "updates": [{
                "column": "Total SF",
                "value": "2,000 SF",
                "confidence": 0.9,
                "reason": "Office component.",
            }],
            "events": [{"type": "property_unavailable"}],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Test Dr", ""],
            ["Property Address", "Total SF"],
            {"mappings": {"total_sf": "Total SF"}},
            _conv("The property is unavailable and has 2,000 SF of office."),
        )
        self.assertIsNone(a._proposal_update_for_column(out, "Total SF"))

    def test_terminal_event_drops_total_sf_not_supported_by_explicit_total(self):
        proposal = {
            "updates": [{
                "column": "Total SF",
                "value": "11,000 SF",
                "confidence": 0.9,
            }],
            "events": [{"type": "property_unavailable"}],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Test Dr", ""],
            ["Property Address", "Total SF"],
            {"mappings": {"total_sf": "Total SF"}},
            _conv("123 Test Dr has 10,000 SF total but is unavailable."),
        )
        self.assertIsNone(a._proposal_update_for_column(out, "Total SF"))

    def test_terminal_event_drops_rent_not_supported_by_explicit_rent(self):
        proposal = {
            "updates": [{
                "column": "Rent/SF /Yr",
                "value": "$15/SF",
                "confidence": 0.9,
            }],
            "events": [{"type": "property_unavailable"}],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Test Dr", ""],
            ["Property Address", "Rent/SF /Yr"],
            {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}},
            _conv("123 Test Dr has asking rent of $12.75/SF/year but is unavailable."),
        )
        self.assertIsNone(a._proposal_update_for_column(out, "Rent/SF /Yr"))

    def test_same_number_different_street_attachment_is_not_target_property(self):
        self.assertFalse(
            a._attachment_can_supply_target_facts(
                "123 Other Ave, Boise, ID. Asking rent $15.00/SF/yr.",
                "123 Test Dr, Boise",
                "The 123 Test Dr property is available; see attached.",
            )
        )

    def test_same_number_and_name_different_suffix_is_not_target_property(self):
        self.assertFalse(
            a._attachment_can_supply_target_facts(
                "123 Test Ave, Boise, ID. Asking rent $15.00/SF/yr.",
                "123 Test Dr, Boise",
                "The 123 Test Dr property is available; see attached.",
            )
        )

    def test_suite_number_is_not_parsed_as_a_street_address(self):
        claims = a._street_claim_spans("Suite 100, 123 Test Dr")
        self.assertEqual([a._claim_identity(claim) for claim in claims], [
            ("123", ("test",), "drive"),
        ])

    def test_target_flyer_ignores_brokerage_footer_address(self):
        proposal = {
            "updates": [{"column": "Rent/SF /Yr", "value": "12", "confidence": 0.9}],
            "events": [],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Test Dr", "Boise", ""],
            ["Property Address", "City", "Rent/SF /Yr"],
            {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}},
            _conv("The 123 Test Dr property is available; see attached."),
            pdf_manifest=[{
                "name": "123 Test Dr flyer.pdf",
                "text": (
                    "123 Test Dr - asking rent $12/SF NNN. "
                    "Brokerage office: 500 Main St, Boise, ID."
                ),
            }],
        )
        self.assertEqual("12.00", a._proposal_update_for_column(out, "Rent/SF /Yr")["value"])

    def test_whole_building_description_remains_total_sf(self):
        phrases = (
            "A 20,000 SF warehouse is available.",
            "The property is a 20,000 SF office building.",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertEqual("20000", a._extract_total_sf_from_text(phrase))

    def test_terminal_event_drops_component_when_real_total_is_also_present(self):
        proposal = {
            "updates": [{"column": "Total SF", "value": "2,000 SF", "confidence": 0.9}],
            "events": [{"type": "property_unavailable"}],
        }
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Test Dr", ""],
            ["Property Address", "Total SF"],
            {"mappings": {"total_sf": "Total SF"}},
            _conv("Total building area is 10,000 SF, including 2,000 SF of office."),
        )
        self.assertIsNone(a._proposal_update_for_column(out, "Total SF"))

    def test_addressless_bound_flyer_can_contain_phone_and_zip_metadata(self):
        self.assertTrue(
            a._attachment_can_supply_target_facts(
                "Call 208-555-1212. Boise, ID 83702. Asking rent $10.50/SF NNN.",
                "2801 Pulaski Hwy",
                "Here is information about 2801 Pulaski Hwy.",
            )
        )

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

    def test_rent_does_not_inherit_monthly_unit_from_following_opex(self):
        self.assertEqual(
            a._extract_rent_sf_yr_from_text(
                "$12.75/SF asking rent, $0.30/SF/month OpEx"
            ),
            "12.75",
        )

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

    def test_dimensioned_singular_drive_in_is_recovered_when_model_omits_it(self):
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Beta Blvd", ""],
            ["Property Address", "Drive Ins"],
            {"mappings": {"drive_ins": "Drive Ins"}},
            _conv("It has 3 phase power and a 12x12 drive in door."),
        )

        self.assertEqual(
            "1",
            a._proposal_update_for_column(out, "Drive Ins")["value"],
        )

        guarded = a._suppress_fabricated_door_counts(
            out,
            _conv("It has 3 phase power and a 12x12 drive in door."),
            ["Property Address", "Drive Ins"],
            {"mappings": {"drive_ins": "Drive Ins"}},
        )
        self.assertEqual(
            "1",
            a._proposal_update_for_column(guarded, "Drive Ins")["value"],
        )

    def test_dimensioned_singular_drive_in_is_not_used_with_mixed_evidence(self):
        proposal = {"updates": [], "events": []}
        out = a._augment_proposal_with_deterministic_extractions(
            proposal,
            ["123 Beta Blvd", ""],
            ["Property Address", "Drive Ins"],
            {"mappings": {"drive_ins": "Drive Ins"}},
            _conv("A 12x12 drive-in door plus two 10x10 drive-ins."),
        )

        self.assertIsNone(a._proposal_update_for_column(out, "Drive Ins"))

    def test_dimensioned_singular_drive_in_requires_current_positive_fact(self):
        phrases = (
            "It does not have one 12x12 drive-in door.",
            "It needs one 12x12 drive-in door.",
            "One 12x12 drive-in door is proposed.",
            "It may have one 12x12 drive-in door.",
            "It used to have one 12x12 drive-in door.",
            "It had one 12x12 drive-in door.",
            "It will have one 12x12 drive-in door.",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertIsNone(
                    a._extract_dimensioned_singular_drive_in_count(phrase)
                )

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

    def test_talk_pricing_over_email_is_not_a_call_request(self):
        # "talk" without phone context must NOT force call_requested — else it
        # nulls a valid auto-reply and pushes an ordinary email to manual review.
        types, proposal = _event_pipeline(
            [], "Can we talk pricing over email instead? It's easier on my end.")
        self.assertNotIn("call_requested", types)
        self.assertEqual(proposal["response_email"], "auto-reply body")

    def test_lets_chat_about_terms_is_not_a_call_request(self):
        types, proposal = _event_pipeline(
            [], "Let's chat about the terms in your reply and go from there.")
        self.assertNotIn("call_requested", types)
        self.assertEqual(proposal["response_email"], "auto-reply body")

    def test_talk_over_the_phone_still_escalates(self):
        # Genuine phone context must still escalate to the operator.
        types, proposal = _event_pipeline(
            [], "Happy to talk over the phone if that's easier for you.")
        self.assertIn("call_requested", types)
        self.assertIsNone(proposal["response_email"])


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
# E4 — Formula-column write guard (apply_proposal_to_sheet).
# "Gross Rent" is a computed cell on the sheet: =(Rent/SF + Ops Ex) * SF / 12.
# The LLM is told (prompt-only) never to propose it, but LIVE testing produced a
# proposal update {column:'Gross Rent', value:'32.00', confidence:0.99} that the
# apply loop happily wrote — clobbering the live formula cell ('' -> '32.00').
# The skip-list in the write loop covered Flyer/Floorplan but had NO formula-column
# guard. These tests drive the deterministic code guard directly, so behavior is
# model-independent (no prompt reliance).
# ---------------------------------------------------------------------------
class FormulaColumnWriteGuardTests(unittest.TestCase):
    def test_is_formula_column_matches_gross_rent_aliases(self):
        for name in ("Gross Rent", "gross rent", "  GROSS RENT  ",
                     "Monthly Gross Rent", "Total Rent", "All-In Rent"):
            self.assertTrue(a._is_formula_column(name),
                            f"{name!r} must be recognized as a formula column")

    def test_is_formula_column_does_not_match_writable_columns(self):
        for name in ("Rent/SF /Yr", "Ops Ex /SF", "Total SF",
                     "Property Address", "Comments", ""):
            self.assertFalse(a._is_formula_column(name),
                             f"{name!r} must NOT be treated as a formula column")

    def _apply(self, header, rowvals, updates):
        """Drive apply_proposal_to_sheet against a FAKE sheets client.

        Only the outer plumbing is stubbed (client acquisition, tab title,
        AI_META ensure/read, batch execution, formula refresh, notes). The write
        loop and its guards run for real, so what lands in `applied`/`skipped`
        and in the captured batch payload reflects production logic exactly.
        """
        sheets = mock.MagicMock()
        proposal = {"updates": updates}
        with mock.patch.object(a, "_sheets_client", return_value=sheets), \
             mock.patch.object(a, "_get_first_tab_title", return_value="Sheet1"), \
             mock.patch.object(a, "_ensure_ai_meta_tab", return_value=None), \
             mock.patch.object(a, "_read_ai_meta_row", return_value=None), \
             mock.patch.object(a, "_append_ai_meta", return_value=None), \
             mock.patch.object(a, "_append_notes_to_comments", return_value=None), \
             mock.patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row",
                        return_value=False), \
             mock.patch.object(a, "_execute_with_retry", return_value={}):
            result = a.apply_proposal_to_sheet(
                uid="u1", client_id="c1", sheet_id="sheet123",
                header=header, rownum=4, current_rowvals=rowvals, proposal=proposal)

        # Recover the ranges the batch write targeted (empty if no write happened).
        ranges = []
        batch = sheets.spreadsheets.return_value.values.return_value.batchUpdate
        if batch.call_args is not None:
            body = batch.call_args.kwargs.get("body", {})
            ranges = [entry.get("range") for entry in body.get("data", [])]
        return result, ranges

    def test_e4_gross_rent_proposal_is_skipped_not_written(self):
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF", "Gross Rent"]
        rowvals = ["Wilson Bldg", "24.00", "8.00", ""]
        result, ranges = self._apply(
            header, rowvals,
            [{"column": "Gross Rent", "value": "32.00", "confidence": 0.99}])
        self.assertEqual(result["applied"], [],
                         "a formula column must never be applied")
        skipped_cols = {(s.get("column"), s.get("reason")) for s in result["skipped"]}
        self.assertIn(("Gross Rent", "formula-column"), skipped_cols)
        self.assertEqual(ranges, [],
                         f"no batch write should target a formula cell: {ranges}")

    def test_e4_gross_rent_skipped_but_writable_column_still_applied(self):
        # Mixed proposal: the formula column is dropped, the real spec is kept.
        header = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF", "Gross Rent"]
        rowvals = ["Wilson Bldg", "", "8.00", ""]
        result, ranges = self._apply(
            header, rowvals,
            [{"column": "Gross Rent", "value": "32.00", "confidence": 0.99},
             {"column": "Rent/SF /Yr", "value": "24.00", "confidence": 0.99}])
        applied_cols = {u["column"] for u in result["applied"]}
        self.assertEqual(applied_cols, {"Rent/SF /Yr"})
        skipped = {(s.get("column"), s.get("reason")) for s in result["skipped"]}
        self.assertIn(("Gross Rent", "formula-column"), skipped)
        # Rent/SF /Yr is column B → B4; Gross Rent is column D → D4 must be absent.
        self.assertTrue(any(r.endswith("B4") for r in ranges), ranges)
        self.assertFalse(any(r.endswith("D4") for r in ranges),
                         f"formula cell D4 must not be in the batch write: {ranges}")


# ---------------------------------------------------------------------------
# Contact opt-out must be a PURE escalation — no sheet writes to the opted-out
# row, no auto-reply (LIVE break adv_optout_with_specs). A broker replies
# "Not interested, remove me. FYI it was going for $18/SF NNN, 12,000 SF." The
# classifier correctly fires contact_optout and nulls response_email, but the
# rent / OpEx / SF specs mentioned in the same breath were still proposed as 3
# sheet writes — silently editing a row the contact just asked us to stop
# touching. A deterministic guard drops every update (and nulls any drafted
# auto-reply) whenever a genuine contact_optout survives, model-independently.
# ---------------------------------------------------------------------------
class ContactOptoutUpdateSuppressionTests(unittest.TestCase):
    OPTOUT_BODY = "Not interested, remove me. FYI it was going for $18/SF NNN, 12,000 SF."

    def test_adv_optout_with_specs_strips_all_updates(self):
        # The break: contact_optout fired, response_email nulled — but 3 spec
        # writes for the opted-out row leaked through.
        proposal = {
            "events": [{"type": "contact_optout", "reason": "not_interested"}],
            "updates": [
                {"column": "Rent/SF /Yr", "value": "18.00"},
                {"column": "Ops Ex /SF", "value": "0.00"},
                {"column": "Total SF", "value": "12000"},
            ],
            "response_email": None,
        }
        out = a._suppress_updates_on_contact_optout(proposal)
        self.assertEqual(out["updates"], [],
                         "no sheet writes may target a row the contact opted out of")

    def test_optout_nulls_any_drafted_autoreply(self):
        # Model-independence: even if the LLM drafted an auto-reply on the opt-out,
        # the guard nulls it so the opt-out is a pure operator escalation.
        proposal = {
            "events": [{"type": "contact_optout", "reason": "unsubscribe"}],
            "updates": [{"column": "Total SF", "value": "12000"}],
            "response_email": "Sure, here are the specs you asked about.",
        }
        out = a._suppress_updates_on_contact_optout(proposal)
        self.assertIsNone(out["response_email"])
        self.assertEqual(out["updates"], [])

    def test_break_body_is_genuine_optout_not_engaged_alternative(self):
        # "remove me" is a real opt-out, NOT a scoped "show me alternatives" — the
        # engaged-alternative guard must not strip it, so the opt-out survives to
        # the update-suppression guard.
        self.assertFalse(a._looks_like_engaged_alternative_request(self.OPTOUT_BODY))

    def test_no_optout_event_is_a_no_op(self):
        # Control: without a contact_optout event the guard leaves updates and the
        # drafted reply untouched.
        proposal = {
            "events": [{"type": "property_unavailable", "reason": "leased"}],
            "updates": [{"column": "Total SF", "value": "12000"}],
            "response_email": "auto-reply body",
        }
        out = a._suppress_updates_on_contact_optout(proposal)
        self.assertEqual(len(out["updates"]), 1)
        self.assertEqual(out["response_email"], "auto-reply body")

    def test_engaged_alternative_keeps_updates_through_pipeline(self):
        # Control (model-independent, production order): a scoped "not interested in
        # that suite, but show me alternatives" reply has its over-fired
        # contact_optout stripped upstream by the engaged-alternative guard, so the
        # extracted specs are preserved — the update-suppression guard is a no-op.
        body = ("I'm not interested in that particular suite, but show me what "
                "else you have nearby. The one I passed on was 12,000 SF.")
        proposal = {
            "events": [{"type": "contact_optout", "reason": "not_interested"}],
            "updates": [{"column": "Total SF", "value": "12000"}],
            "response_email": "auto-reply body",
        }
        proposal = a._suppress_quote_only_events(proposal, _conv(body))
        proposal = a._augment_events_with_deterministic_signals(proposal, _conv(body))
        proposal = a._suppress_updates_on_contact_optout(proposal)
        self.assertEqual(len(proposal["updates"]), 1,
                         "an engaged-alternative lead must keep its extracted specs")


if __name__ == "__main__":
    unittest.main()


class RequiredFieldHeaderAliasTests(unittest.TestCase):
    """LIVE break (golden campaign): a row could never reach 'completed' because
    the missing-required-fields check used default names ('Ops Ex /SF', 'Docks')
    that didn't match Jill's real headers ('Ops Ex / SF', 'Loading Docks')."""
    HEADER = ["Property Address", "Total SF", "Rent/SF /Yr", "Ops Ex / SF",
              "Drive Ins", "Loading Docks", "Ceiling Ht", "Power", "Flyer / Link"]

    def _row(self, **over):
        base = {"Property Address": "200 Interference Rd", "Total SF": "20000",
                "Rent/SF /Yr": "12.00", "Ops Ex / SF": "4.00", "Drive Ins": "1",
                "Loading Docks": "3", "Ceiling Ht": "28", "Power": "1000A",
                "Flyer / Link": "https://x/flyer.pdf"}
        base.update(over)
        return [base.get(h, "") for h in self.HEADER]

    def test_filled_row_with_real_headers_has_no_missing(self):
        # Ops Ex / SF and Loading Docks ARE filled — must NOT be reported missing.
        missing = a.check_missing_required_fields(self._row(), self.HEADER)
        self.assertEqual(missing, [], f"filled row wrongly reported missing: {missing}")

    def test_truly_empty_opex_and_docks_are_reported_missing(self):
        missing = a.check_missing_required_fields(
            self._row(**{"Ops Ex / SF": "", "Loading Docks": ""}), self.HEADER)
        self.assertIn("Ops Ex /SF", missing)
        self.assertIn("Docks", missing)

    def test_missing_flyer_does_not_block_completion(self):
        missing = a.check_missing_required_fields(
            self._row(**{"Flyer / Link": ""}), self.HEADER)
        self.assertNotIn("Flyer / Link", missing)

    def test_missing_rent_blocks_completion(self):
        missing = a.check_missing_required_fields(
            self._row(**{"Rent/SF /Yr": ""}), self.HEADER)
        self.assertIn("Rent/SF /Yr", missing)
