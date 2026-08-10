"""Conservative deterministic fallback contracts for corrected broker facts.

The deterministic layer may fill missing ordinary values, but it must not
replace model-owned values or guess among conflicting/stale candidates. Wrong
non-empty model values remain the model/prompt layer's responsibility.
"""

import json
import os
from decimal import Decimal
from unittest import TestCase, mock

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation import ai_processing as ai
from email_automation.sheet_operations import _build_gross_rent_formula_for_row


HEADER = ["Property Address", "Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Gross Rent"]
ROW = ["100 Example Rd", "", "", "", ""]
CONFIG = {
    "mappings": {
        "property_address": "Property Address",
        "total_sf": "Total SF",
        "rent_sf_yr": "Rent/SF /Yr",
        "ops_ex_sf": "Ops Ex /SF",
        "gross_rent": "Gross Rent",
    },
    "extractionFields": ["total_sf", "rent_sf_yr", "ops_ex_sf"],
    "requiredFields": [],
    "formulaFields": ["gross_rent"],
    "neverRequest": ["rent_sf_yr"],
    "customFields": {},
}
CURRENT_UPDATES = [
    {"column": "Total SF", "value": "32500", "confidence": 0.98},
    {"column": "Rent/SF /Yr", "value": "14.85", "confidence": 0.98},
    {"column": "Ops Ex /SF", "value": "3.65", "confidence": 0.98},
]


def _conversation(text):
    return [{"direction": "inbound", "content": text}]


def _values(proposal):
    return {update["column"]: update["value"] for update in proposal.get("updates", [])}


def _augment(text, updates, pdf_manifest=None, events=None):
    return ai._augment_proposal_with_deterministic_extractions(
        {"updates": [dict(update) for update in updates], "events": list(events or [])},
        ROW,
        HEADER,
        CONFIG,
        _conversation(text),
        pdf_manifest=pdf_manifest,
    )


class CurrentValuePrecedenceTests(TestCase):
    def _propose(self, text, updates):
        response = mock.Mock(
            output_text=json.dumps({
                "updates": updates,
                "events": [],
                "response_email": None,
                "notes": "",
            }),
            usage=None,
            id="response-current-value-precedence",
        )
        fake_client = mock.Mock()
        fake_client.responses.create.return_value = response
        with mock.patch.object(ai, "client", fake_client):
            return ai.propose_sheet_updates(
                uid="local-user",
                client_id="local-client",
                email="broker@example.test",
                sheet_id="local-sheet",
                header=HEADER,
                rownum=3,
                rowvals=ROW,
                thread_id="local-thread",
                conversation=_conversation(text),
                column_config=CONFIG,
                extraction_fields=CONFIG["extractionFields"],
                dry_run=True,
            )

    def test_live_seeded_current_model_values_survive_and_apply(self):
        text = (
            "The old brochure showed 31,000 SF at $15.10/SF, but that was outdated. "
            "The current available suite is 32,500 SF at $14.85/SF. "
            "OpEx is $3.65/SF."
        )
        proposal = self._propose(text, CURRENT_UPDATES)
        expected = {"Total SF": "32500", "Rent/SF /Yr": "14.85", "Ops Ex /SF": "3.65"}
        self.assertEqual(expected, _values(proposal))

        with mock.patch.object(ai, "_sheets_client", return_value=mock.Mock()), \
             mock.patch.object(ai, "_get_first_tab_title", return_value="Sheet1"), \
             mock.patch.object(ai, "_ensure_ai_meta_tab", return_value=None), \
             mock.patch.object(ai, "_read_ai_meta_row", return_value=None), \
             mock.patch.object(ai, "_append_ai_meta", return_value=None), \
             mock.patch.object(ai, "_append_notes_to_comments", return_value=None), \
             mock.patch.object(ai, "_execute_with_retry", return_value={}):
            applied = ai.apply_proposal_to_sheet(
                "local-user", "local-client", "local-sheet", HEADER, 3, ROW, proposal
            )

        self.assertEqual(
            expected,
            {key: applied["rowSnapshotAfter"][key] for key in expected},
        )
        self.assertEqual(
            Decimal("50104.17"),
            ((Decimal("14.85") + Decimal("3.65")) * Decimal("32500") / 12)
            .quantize(Decimal("0.01")),
        )
        self.assertEqual(
            ("E", '=IF(OR(B3="",C3="",D3=""),"",(IFERROR(VALUE(C3),AVERAGE(VALUE(INDEX(SPLIT(C3,"-"),1)),VALUE(INDEX(SPLIT(C3,"-"),2))))+D3)*B3/12)'),
            _build_gross_rent_formula_for_row(HEADER, 3),
        )

    def test_ordinary_single_value_empty_model_proposal_still_fills(self):
        proposal = _augment(
            "The suite is 32,500 SF at $14.85/SF with OpEx of $3.65/SF.",
            [],
        )
        self.assertEqual(
            {"Total SF": "32500", "Rent/SF /Yr": "14.85", "Ops Ex /SF": "3.65"},
            _values(proposal),
        )

    def test_conflicting_empty_model_proposal_abstains_except_unambiguous_opex(self):
        messages = (
            "The previous flyer showed 31,000 SF at $15.10/SF but current "
            "availability is 32,500 SF at $14.85/SF. OpEx is $3.65/SF.",
            "The old rate was $15.10 NNN but the current rate is $14.85 NNN. "
            "The suite is 32,500 SF. OpEx is $3.65/SF.",
            "Former rent was $15.10 gross; revised rent is $14.85 gross. "
            "The suite is 32,500 SF. OpEx is $3.65/SF.",
            "The old rent was 82 cents triple net; revised rent is 75 cents "
            "triple net. The suite is 32,500 SF. OpEx is $3.65/SF.",
            "The old total was $514,375/yr gross on 32,500 SF; the current total "
            "is $482,625/yr gross on 32,500 SF. OpEx is $3.65/SF.",
        )
        for text in messages:
            with self.subTest(text=text):
                self.assertEqual({"Ops Ex /SF": "3.65"}, _values(_augment(text, [])))

    def test_was_now_corrections_abstain_across_supported_rent_shapes(self):
        messages = (
            "The suite was 31,000 SF at $15.10 NNN; now it is 32,500 SF at "
            "$14.85 NNN. OpEx is $3.65/SF.",
            "Rent was $15.10 gross; now it is $14.85 gross. The suite is "
            "32,500 SF. OpEx is $3.65/SF.",
            "Rent was 82 cents triple net; now it is 75 cents triple net. "
            "The suite is 32,500 SF. OpEx is $3.65/SF.",
            "Total rent was $514,375/yr gross on 32,500 SF; now it is "
            "$482,625/yr gross on 32,500 SF. OpEx is $3.65/SF.",
        )
        for text in messages:
            with self.subTest(text=text):
                self.assertEqual({"Ops Ex /SF": "3.65"}, _values(_augment(text, [])))

    def test_numeric_negation_abstains_without_suppressing_relational_not(self):
        for text in (
            "The suite is not approximately 31,000 SF; it is 32,500 SF at "
            "$14.85/SF. OpEx is $3.65/SF.",
            "The suite isn't 31,000 SF; it's 32,500 SF at $14.85/SF. "
            "OpEx is $3.65/SF.",
        ):
            with self.subTest(text=text):
                self.assertEqual({"Ops Ex /SF": "3.65"}, _values(_augment(text, [])))

        for text in (
            "The suite is 32,500 SF at $14.85/SF, not including OpEx of $3.65/SF.",
            "The asking rent does not currently include OpEx. The suite is "
            "32,500 SF at $14.85/SF with OpEx of $3.65/SF.",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    {"Total SF": "32500", "Rent/SF /Yr": "14.85", "Ops Ex /SF": "3.65"},
                    _values(_augment(text, [])),
                )

    def test_cue_bearing_terminal_event_still_strips_unsupported_facts(self):
        proposal = _augment(
            "The old brochure showed 10,000 SF at $12.75/SF; the property is unavailable.",
            [
                {"column": "Total SF", "value": "11000", "confidence": 0.98},
                {"column": "Rent/SF /Yr", "value": "15", "confidence": 0.98},
            ],
            events=[{"type": "property_unavailable"}],
        )
        self.assertNotIn("Total SF", _values(proposal))
        self.assertNotIn("Rent/SF /Yr", _values(proposal))

    def test_stale_only_empty_model_proposal_abstains(self):
        proposal = _augment(
            "The former brochure showed 31,000 SF at $15.10/SF; those figures are obsolete.",
            [],
        )
        self.assertNotIn("Total SF", _values(proposal))
        self.assertNotIn("Rent/SF /Yr", _values(proposal))

    def test_model_values_are_never_overwritten_by_competing_facts(self):
        messages = (
            "70,000 SF at $9.00/SF is available at 200 Other Rd; 32,500 SF at "
            "$14.85/SF is available at 100 Example Rd. OpEx is $3.65/SF.",
            "West Building has 70,000 SF at $9.00/SF; East Building has 32,500 "
            "SF at $14.85/SF. OpEx is $3.65/SF.",
            "At 100 Example Rd, Suite A has 70,000 SF at $9.00/SF; Suite B has "
            "32,500 SF at $14.85/SF. OpEx is $3.65/SF.",
            "The previous flyer showed 31,000 SF at $15.10/SF but current "
            "availability is 32,500 SF at $14.85/SF. OpEx is $3.65/SF.",
            "The suite isn't 31,000 SF; it's 32,500 SF at $14.85/SF. OpEx is $3.65/SF.",
            "The old rate was $15.10 NNN but the current rate is $14.85 NNN. "
            "The suite is 32,500 SF. OpEx is $3.65/SF.",
            "Former rent was $15.10 gross; revised rent is $14.85 gross. "
            "The suite is 32,500 SF. OpEx is $3.65/SF.",
        )
        expected = {"Total SF": "32500", "Rent/SF /Yr": "14.85", "Ops Ex /SF": "3.65"}
        for text in messages:
            with self.subTest(text=text):
                self.assertEqual(expected, _values(_augment(text, CURRENT_UPDATES)))

    def test_seeded_total_sf_survives_unrelated_component_layout(self):
        proposal = _augment(
            "200 Other Rd has 70,000 SF including 32,500 SF of warehouse at "
            "$9.00/SF. OpEx is $3.65/SF.",
            CURRENT_UPDATES,
        )
        self.assertEqual(
            {"Total SF": "32500", "Rent/SF /Yr": "14.85", "Ops Ex /SF": "3.65"},
            _values(proposal),
        )

    def test_component_only_model_total_remains_sanitized(self):
        proposal = _augment(
            "The property has about 2,000 SF of office.",
            [{"column": "Total SF", "value": "2000", "confidence": 0.98}],
        )
        self.assertNotIn("Total SF", _values(proposal))

    def test_numeric_formatting_and_opex_correction_remain_deterministic(self):
        rent = _augment(
            "The asking rent is $12/SF.",
            [{"column": "Rent/SF /Yr", "value": "12", "confidence": 0.98}],
        )
        self.assertEqual("12.00", _values(rent)["Rent/SF /Yr"])

        opex = _augment(
            "CAM is $4.25/SF, corrected to $3.90/SF.",
            [{"column": "Ops Ex /SF", "value": "4.25", "confidence": 0.98}],
        )
        self.assertEqual("3.90", _values(opex)["Ops Ex /SF"])

    def test_monthly_rent_normalizes_but_nonmonthly_conflict_stays_model_owned(self):
        monthly = _augment(
            "Asking rent: $1.12/SF NNN monthly. Ops Ex: $0.27/SF monthly.",
            [{"column": "Rent/SF /Yr", "value": "1.12", "confidence": 0.98}],
        )
        self.assertEqual("13.44", _values(monthly)["Rent/SF /Yr"])

        for text, raw_value, annual_value in (
            ("Asking rent is 1.10 NNN.", "1.10", "13.20"),
            ("Asking rent is 82 cents triple net.", "0.82", "9.84"),
        ):
            with self.subTest(text=text):
                normalized = _augment(
                    text,
                    [{"column": "Rent/SF /Yr", "value": raw_value, "confidence": 0.98}],
                )
                self.assertEqual(annual_value, _values(normalized)["Rent/SF /Yr"])

        conflicting = _augment(
            "The asking rent is $15.10/SF.",
            [{"column": "Rent/SF /Yr", "value": "14.85", "confidence": 0.98}],
        )
        self.assertEqual("14.85", _values(conflicting)["Rent/SF /Yr"])

    def test_correction_discourse_blocks_target_pdf_fallback(self):
        text = (
            "Old brochure: 31,000 SF at $15.10/SF; current: 32,500 SF at "
            "$14.85/SF. OpEx is $3.65/SF."
        )
        pdf = [{
            "name": "100 Example Rd brochure.pdf",
            "text": "100 Example Rd - 31,000 SF - asking rent $15.10/SF.",
        }]
        expected = {"Total SF": "32500", "Rent/SF /Yr": "14.85", "Ops Ex /SF": "3.65"}
        self.assertEqual(expected, _values(_augment(text, CURRENT_UPDATES, pdf)))
        self.assertEqual({"Ops Ex /SF": "3.65"}, _values(_augment(text, [], pdf)))
