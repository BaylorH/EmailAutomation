import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import patch

from email_automation.ai_processing import apply_proposal_to_sheet


# ---------------------------------------------------------------------------
# Fakes: patch ONLY the Google Sheets datastore boundary. The unit under test
# (apply_proposal_to_sheet) runs for real.
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
                "values": [
                    [
                        "rowNumber",
                        "columnName",
                        "last_ai_value",
                        "last_ai_write_iso",
                        "human_override",
                        "rowAnchor",
                    ],
                ]
            })
        return FakeRequest({"values": []})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return FakeRequest({})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
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


class CorePropertyExtractionBadPlaceholderTests(unittest.TestCase):
    """core.property_extraction / bad_placeholder.

    apply_proposal_to_sheet is the owner of property-extraction sheet writes. A
    proposal whose value is an unresolved template placeholder (e.g. "[NAME]",
    "[BROKER]") must NEVER be written verbatim into a client Google Sheet - the
    same class of leak the outbound-email path already rejects via
    outbound_safety.find_unresolved_placeholders. This test drives the real
    apply_proposal_to_sheet into an empty target cell (no human value, no prior
    AI_META - so the ONLY thing that can stop the write is a placeholder guard)
    and proves the placeholder is skipped with reason 'placeholder-value' and no
    datastore write happens. The negative control feeds an identical proposal
    carrying a genuine resolved value and proves it DOES write - so the block is
    caused by placeholder detection, not by the write being unconditionally
    refused.
    """

    HEADER = ["Property Address", "City", "Broker"]
    ROWNUM = 3
    # Broker column empty -> a first-time write, nothing else can block it.
    CURRENT_ROW = ["404 New Way", "Dallas", ""]

    def _run(self, value):
        proposal = {
            "updates": [
                {
                    "column": "Broker",
                    "value": value,
                    "confidence": 0.95,
                    "reason": "Extracted broker of record from the reply.",
                }
            ]
        }
        fake_sheets = FakeSheets()
        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid-1",
                "client-1",
                "sheet-1",
                self.HEADER,
                self.ROWNUM,
                self.CURRENT_ROW,
                proposal,
            )
        return fake_sheets, result

    def test_placeholder_value_is_never_written_but_real_value_is(self):
        # --- MAIN CASE: the proposal value is an unresolved placeholder.
        fake_sheets, result = self._run("[NAME]")

        self.assertEqual([], result["applied"], "A placeholder value must not be applied to the sheet.")
        self.assertEqual(1, len(result["skipped"]))
        skipped = result["skipped"][0]
        self.assertEqual("Broker", skipped["column"])
        self.assertEqual("placeholder-value", skipped["reason"])
        # The Broker cell stays empty - the literal "[NAME]" never landed.
        self.assertEqual("", result["rowSnapshotAfter"]["Broker"])
        # And NO datastore write was issued at all.
        self.assertEqual(
            [],
            fake_sheets.values_api.batch_update_calls,
            "No batchUpdate should be issued when the only update is a placeholder.",
        )

        # --- NEGATIVE CONTROL: identical proposal shape carrying a real,
        # resolved value. This MUST write, proving the guard rejects the
        # placeholder specifically rather than blocking the column outright.
        fake_ctrl, control = self._run("Karsen Ellsworth")

        self.assertEqual([], control["skipped"])
        self.assertEqual(1, len(control["applied"]))
        applied = control["applied"][0]
        self.assertEqual("Broker", applied["column"])
        self.assertEqual("Sheet1!C3", applied["range"])
        self.assertEqual("Karsen Ellsworth", applied["newValue"])
        self.assertEqual("Karsen Ellsworth", control["rowSnapshotAfter"]["Broker"])
        self.assertEqual(1, len(fake_ctrl.values_api.batch_update_calls))
        written = fake_ctrl.values_api.batch_update_calls[0]["data"][0]
        self.assertEqual("Sheet1!C3", written["range"])
        self.assertEqual([["Karsen Ellsworth"]], written["values"])


if __name__ == "__main__":
    unittest.main()
