import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import unittest
from unittest.mock import patch

from email_automation.sheet_operations import insert_property_row_above_divider


# ---------------------------------------------------------------------------
# Fakes: the boundary is the Google Sheets client (passed in as `sheets`). The
# unit under test (insert_property_row_above_divider) runs for real.
# ---------------------------------------------------------------------------
class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeValues:
    def __init__(self, column_a_rows):
        self._column_a_rows = column_a_rows
        self.update_calls = []

    def get(self, spreadsheetId=None, range=None, **kwargs):
        # Column A read used to locate the NON-VIABLE divider.
        return FakeRequest({"values": self._column_a_rows})

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
        self.update_calls.append({"range": range, "body": body})
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self, values):
        self._values = values
        self.batch_update_calls = []

    def values(self):
        return self._values

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return FakeRequest({})


class FakeSheets:
    def __init__(self, column_a_rows):
        self.values_api = FakeValues(column_a_rows)
        self.spreadsheets_api = FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


class CoreSheetUpdateBadPlaceholderTests(unittest.TestCase):
    """core.sheet_update / bad_placeholder.

    insert_property_row_above_divider (with app.py:launch as its only caller)
    writes a freshly-built property row straight into a client Google Sheet. An
    unresolved template placeholder (e.g. "[NAME]") arriving in values_by_header
    must not be written verbatim into a cell - the same leak class the outbound
    path already blocks. This test drives the real function and proves the
    placeholder cell is sanitized to empty in the row actually written, while a
    negative control with a genuine value writes it through unchanged - proving
    the guard targets the placeholder, not the column.
    """

    HEADER = ["Property Address", "City", "Broker"]
    # Column A has two live rows then the NON-VIABLE divider at row 4.
    COLUMN_A_ROWS = [
        ["Property Address"],
        ["101 Old St"],
        ["202 Mid Ave"],
        ["NON-VIABLE"],
    ]

    def _run(self, broker_value):
        values_by_header = {
            "property address": "404 New Way",
            "city": "Dallas",
            "broker": broker_value,
        }
        fake_sheets = FakeSheets(self.COLUMN_A_ROWS)
        with patch("email_automation.sheet_operations._read_header_row2", return_value=self.HEADER), \
             patch("email_automation.sheet_operations._first_sheet_props", return_value=(0, "Sheet1")), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            new_row = insert_property_row_above_divider(
                fake_sheets, "sheet-1", "Sheet1", values_by_header
            )
        return fake_sheets, new_row

    def test_placeholder_cell_is_sanitized_but_real_value_written(self):
        # --- MAIN CASE: Broker is an unresolved placeholder.
        fake_sheets, _ = self._run("[NAME]")

        self.assertEqual(1, len(fake_sheets.values_api.update_calls))
        written_row = fake_sheets.values_api.update_calls[0]["body"]["values"][0]
        # Row order follows HEADER: [address, city, broker].
        self.assertEqual("404 New Way", written_row[0])
        self.assertEqual("Dallas", written_row[1])
        self.assertEqual(
            "",
            written_row[2],
            "The literal placeholder '[NAME]' must never be written into the Broker cell.",
        )

        # --- NEGATIVE CONTROL: a genuine broker name writes through unchanged.
        fake_ctrl, _ = self._run("Karsen Ellsworth")
        ctrl_row = fake_ctrl.values_api.update_calls[0]["body"]["values"][0]
        self.assertEqual("404 New Way", ctrl_row[0])
        self.assertEqual("Dallas", ctrl_row[1])
        self.assertEqual("Karsen Ellsworth", ctrl_row[2])


if __name__ == "__main__":
    unittest.main()
