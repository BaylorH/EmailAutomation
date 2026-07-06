"""
Surface B — extraction robustness across broker spec formats.

Each test pins a confirmed extraction/apply defect found by the deterministic +
live find agents, driving the REAL extraction/apply functions in
email_automation/ai_processing.py with the VERBATIM broker phrasing and asserting
the correct cell write (or the correct suppression).

No Firestore / Sheets / Graph / OpenAI network calls happen: deterministic guards
are pure over (proposal, conversation); the apply-path tests patch ONLY the Google
Sheets datastore boundary (a fake in-memory sheet) — the unit under test runs for
real. Zero sends, zero live-sheet writes.
"""

import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation.ai_processing import (
    apply_proposal_to_sheet,
    _extract_rent_sf_yr_from_text,
    _augment_proposal_with_deterministic_extractions as augment_extractions,
    _augment_proposal_opex_basis as augment_opex,
    _augment_events_with_deterministic_signals as augment_events,
    _looks_like_requirements_mismatch_nonviable,
)


def _inbound(text):
    return [{"direction": "inbound", "content": text}]


def _types(proposal):
    return [(e or {}).get("type") for e in (proposal.get("events") or [])]


# ---------------------------------------------------------------------------
# Fake Google Sheets datastore boundary (mirrors the bad-placeholder rubric
# test). apply_proposal_to_sheet runs for real; only the Sheets client is faked.
# ---------------------------------------------------------------------------
class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeValues:
    def __init__(self):
        self.batch_update_calls = []

    def get(self, spreadsheetId=None, range=None, **kwargs):
        if range and range.startswith("AI_META!"):
            return FakeRequest({
                "values": [[
                    "rowNumber", "columnName", "last_ai_value",
                    "last_ai_write_iso", "human_override", "rowAnchor",
                ]]
            })
        return FakeRequest({"values": []})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values

    def get(self, spreadsheetId=None, **kwargs):
        return FakeRequest({
            "sheets": [
                {"properties": {"title": "Sheet1", "sheetId": 0}},
                {"properties": {"title": "AI_META", "sheetId": 1}},
            ]
        })

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        return FakeRequest({})


class FakeSheets:
    def __init__(self):
        self.values_api = FakeValues()
        self.spreadsheets_api = FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


# ===========================================================================
# BUG 1 — apply-placeholder: a data placeholder ("TBD", "N/A", "pending",
# "TBC", "To follow", "ask landlord") is NOT data and must never be written
# verbatim into a client sheet cell.
# ===========================================================================
class Bug1ApplyPlaceholder(unittest.TestCase):
    HEADER = ["Property Address", "City", "Ceiling Ht"]
    ROWNUM = 5
    CURRENT_ROW = ["500 Main St", "Dallas", ""]  # empty target -> only a guard can block

    def _run(self, value):
        proposal = {"updates": [{
            "column": "Ceiling Ht",
            "value": value,
            "confidence": 0.95,
            "reason": "Extracted ceiling height from the reply.",
        }]}
        fake_sheets = FakeSheets()
        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid-1", "client-1", "sheet-1",
                self.HEADER, self.ROWNUM, self.CURRENT_ROW, proposal,
            )
        return fake_sheets, result

    def test_placeholder_values_never_written(self):
        for placeholder in ["TBD", "N/A", "n/a", "pending", "TBC", "To follow", "ask landlord"]:
            with self.subTest(placeholder=placeholder):
                fake_sheets, result = self._run(placeholder)
                self.assertEqual([], result["applied"], f"{placeholder!r} must not be applied")
                self.assertEqual(1, len(result["skipped"]))
                self.assertEqual("placeholder-value", result["skipped"][0]["reason"])
                self.assertEqual("", result["rowSnapshotAfter"]["Ceiling Ht"])
                self.assertEqual([], fake_sheets.values_api.batch_update_calls,
                                 f"No batchUpdate should be issued for placeholder {placeholder!r}")

    def test_real_value_still_writes(self):
        # NEGATIVE CONTROL — a genuine spec value writes, proving the guard is
        # placeholder-specific rather than a blanket block on the column.
        fake_sheets, result = self._run("24")
        self.assertEqual([], result["skipped"])
        self.assertEqual(1, len(result["applied"]))
        self.assertEqual("Sheet1!C5", result["applied"][0]["range"])
        self.assertEqual("24", result["rowSnapshotAfter"]["Ceiling Ht"])
        self.assertEqual(1, len(fake_sheets.values_api.batch_update_calls))


# ===========================================================================
# BUG 2 — augment-mixed-basis: when the deterministic layer annualizes a
# monthly rent, the OPEX must not be left on a monthly basis.
# ===========================================================================
class Bug2MixedBasis(unittest.TestCase):
    TEXT = "Rent $0.82 NNN with opex $0.21/SF/mo"
    HEADER = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
    ROWVALS = ["12 Trade Way", "", ""]
    CONFIG = {"mappings": {"rent_sf_yr": "Rent/SF /Yr", "ops_ex_sf": "Ops Ex /SF"}}

    def test_rent_and_opex_end_on_same_annual_basis(self):
        # LLM proposed both on a monthly basis.
        proposal = {"updates": [
            {"column": "Rent/SF /Yr", "value": "0.82", "confidence": 0.8},
            {"column": "Ops Ex /SF", "value": "0.21", "confidence": 0.8},
        ]}
        proposal = augment_extractions(proposal, self.ROWVALS, self.HEADER, self.CONFIG, _inbound(self.TEXT))
        proposal = augment_opex(proposal, self.ROWVALS, self.HEADER, self.CONFIG, _inbound(self.TEXT))

        rent = [u["value"] for u in proposal["updates"] if u["column"] == "Rent/SF /Yr"]
        opex = [u["value"] for u in proposal["updates"] if u["column"] == "Ops Ex /SF"]
        self.assertEqual(["9.84"], rent, "monthly rent must be annualized")
        self.assertEqual(["2.52"], opex, "opex must be annualized to match the rent basis")


# ===========================================================================
# BUG 3 / BUG 4 — rent-extract-drop: rates the fallback silently dropped.
# ===========================================================================
class Bug3And4RentExtractDrop(unittest.TestCase):
    def test_cents_triple_net_annualized(self):
        # '82 cents triple net' -> $0.82/SF/mo NNN -> 9.84/yr
        self.assertEqual("9.84", _extract_rent_sf_yr_from_text("82 cents triple net"))

    def test_total_annual_rent_over_sf(self):
        # '$105,000/yr gross on 12,000 SF' -> 105000 / 12000 = 8.75/SF/yr
        self.assertEqual("8.75", _extract_rent_sf_yr_from_text("$105,000/yr gross on 12,000 SF"))


# ===========================================================================
# BUG 5 — clear-height 'under joist' is a measurement reference, NOT a
# below-spec mismatch. A benign spec reply must emit NO property_unavailable.
# ===========================================================================
class Bug5UnderJoist(unittest.TestCase):
    TEXT = "Clear height is 22 ft 9 in under joist. Everything else on the flyer is current."

    def test_helper_does_not_flag_mismatch(self):
        self.assertFalse(_looks_like_requirements_mismatch_nonviable(self.TEXT))

    def test_no_property_unavailable_event(self):
        proposal = augment_events({"events": []}, _inbound(self.TEXT))
        self.assertNotIn("property_unavailable", _types(proposal))

    def test_genuine_below_spec_still_flagged(self):
        # NEGATIVE CONTROL — a real below-spec height still fires.
        self.assertTrue(_looks_like_requirements_mismatch_nonviable(
            "Clear height is only 14', below your requirement."
        ))


# ===========================================================================
# BUG 6 — power-extraction near-miss control: phone digits must never become
# a rent spec. Deterministic control (call_requested is LLM/prompt-layer).
# ===========================================================================
class Bug6PhoneDigitsNotRent(unittest.TestCase):
    TEXT = "Best to reach me at 410-555-0200. Building has 400A service, 22' clear, 30,000 SF."

    def test_phone_digits_do_not_become_rent(self):
        self.assertIsNone(_extract_rent_sf_yr_from_text(self.TEXT))


# ===========================================================================
# BUG 7 — gross lease basis: broker stated NO separate opex figure; a
# fabricated Ops Ex = 0 must be stripped, rent kept.
# ===========================================================================
class Bug7GrossBasisFabricatedOpex(unittest.TestCase):
    TEXT = "This one is quoted at $15/SF gross - all in, no separate opex pass-through. 35,000 SF."
    HEADER = ["Property Address", "Rent/SF /Yr", "Ops Ex /SF"]
    ROWVALS = ["9 Depot Rd", "", ""]
    CONFIG = {"mappings": {"rent_sf_yr": "Rent/SF /Yr", "ops_ex_sf": "Ops Ex /SF"}}

    def test_fabricated_zero_opex_stripped_rent_kept(self):
        proposal = {"updates": [
            {"column": "Rent/SF /Yr", "value": "15", "confidence": 0.9},
            {"column": "Ops Ex /SF", "value": "0", "confidence": 0.8},
        ]}
        proposal = augment_opex(proposal, self.ROWVALS, self.HEADER, self.CONFIG, _inbound(self.TEXT))
        cols = [u["column"] for u in proposal["updates"]]
        self.assertNotIn("Ops Ex /SF", cols, "fabricated gross-basis opex must be stripped")
        self.assertIn("Rent/SF /Yr", cols, "rent must be preserved")

    def test_real_opex_not_stripped(self):
        # NEGATIVE CONTROL — a stated opex figure survives.
        text = "Asking $15/SF NNN with opex around $3.10/SF. 35,000 SF."
        proposal = {"updates": [
            {"column": "Rent/SF /Yr", "value": "15", "confidence": 0.9},
            {"column": "Ops Ex /SF", "value": "3.10", "confidence": 0.8},
        ]}
        proposal = augment_opex(proposal, self.ROWVALS, self.HEADER, self.CONFIG, _inbound(text))
        opex = [u["value"] for u in proposal["updates"] if u["column"] == "Ops Ex /SF"]
        self.assertEqual(["3.10"], opex)


if __name__ == "__main__":
    unittest.main()
