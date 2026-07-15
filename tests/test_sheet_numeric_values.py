import os
import unittest
from unittest import mock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import ai_processing
from email_automation.column_config import sheet_values_equal_for_column


class NumericSheetValueTests(unittest.TestCase):
    def test_numeric_zero_is_not_equal_to_blank(self):
        self.assertFalse(sheet_values_equal_for_column("Total SF", 0, ""))

    def _apply(self, *, header, rowvals, update, meta_rows=None):
        sheets = mock.MagicMock()
        with mock.patch.object(ai_processing, "_sheets_client", return_value=sheets), \
             mock.patch.object(ai_processing, "_get_first_tab_title", return_value="Properties"), \
             mock.patch.object(ai_processing, "_ensure_ai_meta_tab", return_value=None), \
             mock.patch.object(ai_processing, "_load_ai_meta_rows", return_value=meta_rows or []), \
             mock.patch.object(ai_processing, "_append_ai_meta", return_value=None), \
             mock.patch.object(ai_processing, "_append_notes_to_comments", return_value=None), \
             mock.patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False), \
             mock.patch.object(ai_processing, "_execute_with_retry", return_value={}):
            result = ai_processing.apply_proposal_to_sheet(
                uid="u1",
                client_id="c1",
                sheet_id="sheet-1",
                header=header,
                rownum=3,
                current_rowvals=rowvals,
                proposal={"updates": [update]},
            )

        batch_call = sheets.spreadsheets.return_value.values.return_value.batchUpdate.call_args
        body = batch_call.kwargs["body"] if batch_call else None
        return result, body

    def test_currency_update_is_written_as_number_with_raw_input_mode(self):
        result, body = self._apply(
            header=["Property Address", "Rent/SF /Yr"],
            rowvals=["123 Industrial Ave", ""],
            update={"column": "Rent/SF /Yr", "value": "$15.00", "confidence": 0.99},
        )

        self.assertEqual("RAW", body["valueInputOption"])
        self.assertEqual([[15.0]], body["data"][0]["values"])
        self.assertEqual("$15.00", result["applied"][0]["newValue"])

    def test_number_update_with_commas_is_written_as_number(self):
        result, body = self._apply(
            header=["Property Address", "Total SF"],
            rowvals=["123 Industrial Ave", ""],
            update={"column": "Total SF", "value": "15,000", "confidence": 0.99},
        )

        self.assertEqual([[15000]], body["data"][0]["values"])
        self.assertEqual("15,000", result["applied"][0]["newValue"])

    def test_formatted_currency_matches_prior_numeric_ai_value_for_override_guard(self):
        meta_rows = [
            ["rowNumber", "columnName", "last_ai_value", "last_ai_write_iso", "human_override", "rowAnchor"],
            [3, "Rent/SF /Yr", 15.0, "2026-07-15T00:00:00Z", False, "123 Industrial Ave"],
        ]
        result, body = self._apply(
            header=["Property Address", "Rent/SF /Yr"],
            rowvals=["123 Industrial Ave", "$15.00"],
            update={"column": "Rent/SF /Yr", "value": "16.00", "confidence": 0.99},
            meta_rows=meta_rows,
        )

        self.assertEqual([], [item for item in result["skipped"] if item["reason"] == "human-override"])
        self.assertEqual([[16.0]], body["data"][0]["values"])

    def test_no_change_audit_preserves_original_string_value(self):
        result, body = self._apply(
            header=["Property Address", "Rent/SF /Yr"],
            rowvals=["123 Industrial Ave", "$15.00"],
            update={"column": "Rent/SF /Yr", "value": "15.00", "confidence": 0.99},
        )

        self.assertIsNone(body)
        self.assertEqual("15.00", result["skipped"][0]["newValue"])


if __name__ == "__main__":
    unittest.main()
