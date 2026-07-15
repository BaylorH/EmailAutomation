import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import sheets


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeValues:
    def __init__(self, rows=None):
        self.rows = rows or [
            ["Property Address", "Listing Brokers Comments ", "Flyer / Link"],
            [
                "404 Tour Stack Blvd",
                "Broker offered two tour windows and is confirming power.",
                "https://example.com/flyer.pdf",
            ],
        ]
        self.batch_body = None

    def get(self, *, spreadsheetId, range, **kwargs):
        if range.endswith("!A1:A1"):
            return FakeRequest({"values": [["SiteSift Proof Campaign"]]})
        if range.endswith("!A2:ZZZ"):
            return FakeRequest({"values": self.rows})
        raise AssertionError(f"Unexpected values.get range: {range}")

    def batchUpdate(self, *, spreadsheetId, body):
        self.batch_body = body
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self, rows=None):
        self.batch_body = None
        self.values_api = FakeValues(rows)

    def get(self, *, spreadsheetId):
        return FakeRequest({
            "sheets": [{
                "properties": {
                    "sheetId": 123,
                    "title": "Properties",
                }
            }]
        })

    def values(self):
        return self.values_api

    def batchUpdate(self, *, spreadsheetId, body):
        self.batch_body = body
        return FakeRequest({})


class FakeSheetsService:
    def __init__(self, rows=None):
        self.spreadsheet_api = FakeSpreadsheets(rows)

    def spreadsheets(self):
        return self.spreadsheet_api


class SheetFormattingTests(unittest.TestCase):
    def test_client_team_comment_slash_alias_still_wraps(self):
        self.assertTrue(sheets.is_wrapped_notes_column("Client / Team Comments"))

    def test_autosize_wraps_notes_columns_without_blocking_processing(self):
        fake_service = FakeSheetsService()

        with patch.object(sheets, "_sheets_client", return_value=fake_service):
            sheets.format_sheet_columns_autosize_with_exceptions(
                "sheet-1",
                ["Property Address", "Listing Brokers Comments ", "Flyer / Link"],
            )

        requests = fake_service.spreadsheet_api.batch_body["requests"]
        wrap_requests = [r["repeatCell"] for r in requests if "repeatCell" in r]
        note_wrap = wrap_requests[1]["cell"]["userEnteredFormat"]["wrapStrategy"]
        flyer_wrap = wrap_requests[2]["cell"]["userEnteredFormat"]["wrapStrategy"]

        self.assertEqual("WRAP", note_wrap)
        self.assertEqual("CLIP", flyer_wrap)

    def test_currency_columns_receive_persistent_dollar_number_format(self):
        rows = [
            ["Property Address", "Rent/SF /Yr", "Ops Ex / SF", "Gross Rent"],
            ["123 Industrial Ave", "15.00", "3.00", 6500],
        ]
        fake_service = FakeSheetsService(rows)

        with patch.object(sheets, "_sheets_client", return_value=fake_service):
            sheets.format_sheet_columns_autosize_with_exceptions(
                "sheet-1",
                rows[0],
            )

        requests = fake_service.spreadsheet_api.batch_body["requests"]
        by_column = {
            request["repeatCell"]["range"]["startColumnIndex"]: request["repeatCell"]
            for request in requests
            if "repeatCell" in request
        }
        for column_index in (1, 2, 3):
            repeat = by_column[column_index]
            self.assertEqual(
                {"type": "CURRENCY", "pattern": "$#,##0.00"},
                repeat["cell"]["userEnteredFormat"]["numberFormat"],
            )
            self.assertIn("userEnteredFormat.numberFormat", repeat["fields"])

    def test_currency_formatting_converts_legacy_text_numbers_without_touching_formula(self):
        rows = [
            ["Property Address", "Rent/SF /Yr", "Ops Ex / SF", "Gross Rent"],
            ["123 Industrial Ave", "15.00", "$3.00", 6500],
            ["456 Warehouse Rd", 10.0, "Call for pricing", 7200],
        ]
        fake_service = FakeSheetsService(rows)

        with patch.object(sheets, "_sheets_client", return_value=fake_service):
            sheets.format_sheet_columns_autosize_with_exceptions(
                "sheet-1",
                rows[0],
            )

        value_batch = fake_service.spreadsheet_api.values_api.batch_body
        self.assertEqual("RAW", value_batch["valueInputOption"])
        self.assertEqual(
            [
                {"range": "Properties!B3", "values": [[15.0]]},
                {"range": "Properties!C3", "values": [[3.0]]},
            ],
            value_batch["data"],
        )
        self.assertFalse(
            any(entry["range"].startswith("Properties!D") for entry in value_batch["data"]),
            "Gross Rent is a formula column and must never be rewritten as a value",
        )


if __name__ == "__main__":
    unittest.main()
