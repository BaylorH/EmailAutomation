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
    def get(self, *, spreadsheetId, range):
        if range.endswith("!A1:A1"):
            return FakeRequest({"values": [["SiteSift Proof Campaign"]]})
        if range.endswith("!A2:ZZZ"):
            return FakeRequest({
                "values": [
                    ["Property Address", "Listing Brokers Comments ", "Flyer / Link"],
                    [
                        "404 Tour Stack Blvd",
                        "Broker offered two tour windows and is confirming power.",
                        "https://example.com/flyer.pdf",
                    ],
                ]
            })
        raise AssertionError(f"Unexpected values.get range: {range}")


class FakeSpreadsheets:
    def __init__(self):
        self.batch_body = None

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
        return FakeValues()

    def batchUpdate(self, *, spreadsheetId, body):
        self.batch_body = body
        return FakeRequest({})


class FakeSheetsService:
    def __init__(self):
        self.spreadsheet_api = FakeSpreadsheets()

    def spreadsheets(self):
        return self.spreadsheet_api


class SheetFormattingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
