import unittest
import os
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation.ai_processing import apply_proposal_to_sheet


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeValues:
    def __init__(self):
        self.batch_update_calls = []
        self.append_calls = []

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
                    [
                        "3",
                        "Total SF",
                        "10,000",
                        "2026-06-01T00:00:00Z",
                        "False",
                        "101 Old St, Dallas",
                    ],
                ]
            })
        return FakeRequest({"values": []})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return FakeRequest({})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
        self.append_calls.append({
            "range": range,
            "body": body,
        })
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


class FakeSheets:
    def __init__(self):
        self.values_api = FakeValues()
        self.spreadsheets_api = FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


class AiMetaRowIdentityTests(unittest.TestCase):
    def test_stale_ai_meta_for_same_row_number_does_not_block_moved_property_update(self):
        fake_sheets = FakeSheets()
        header = ["Property Address", "City", "Total SF"]
        current_row = ["404 New Way", "Dallas", "5,000"]
        proposal = {
            "updates": [
                {
                    "column": "Total SF",
                    "value": "6,000",
                    "confidence": 0.95,
                    "reason": "Broker corrected the current property's square footage.",
                }
            ]
        }

        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid-1",
                "client-1",
                "sheet-1",
                header,
                3,
                current_row,
                proposal,
            )

        self.assertEqual([], result["skipped"])
        self.assertEqual("Total SF", result["applied"][0]["column"])
        self.assertEqual("Sheet1!C3", result["applied"][0]["range"])
        self.assertEqual("6,000", result["applied"][0]["newValue"])
        self.assertEqual(1, len(fake_sheets.values_api.append_calls))
        appended_row = fake_sheets.values_api.append_calls[0]["body"]["values"][0]
        self.assertEqual("404 New Way, Dallas", appended_row[5])


if __name__ == "__main__":
    unittest.main()
