import unittest
import os
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation.ai_processing import (
    _append_ai_meta,
    _ensure_ai_meta_tab,
    apply_proposal_to_sheet,
)


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeValues:
    def __init__(self, ai_meta_rows=None):
        self.batch_update_calls = []
        self.append_calls = []
        self.get_calls = []
        self.ai_meta_rows = ai_meta_rows or [
            [
                "3",
                "Total SF",
                "10,000",
                "2026-06-01T00:00:00Z",
                "False",
                "101 Old St, Dallas",
            ],
        ]

    def get(self, spreadsheetId=None, range=None, **kwargs):
        self.get_calls.append({"spreadsheetId": spreadsheetId, "range": range})
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
                    *self.ai_meta_rows,
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
        self.batch_update_calls = []
        self.get_calls = []

    def values(self):
        return self._values

    def get(self, spreadsheetId=None, **kwargs):
        self.get_calls.append({"spreadsheetId": spreadsheetId})
        return FakeRequest({
            "sheets": [
                {"properties": {"title": "Sheet1", "sheetId": 0}},
                {"properties": {"title": "AI_META", "sheetId": 1}},
            ]
        })

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return FakeRequest({})


class FakeSheets:
    def __init__(self, ai_meta_rows=None):
        self.values_api = FakeValues(ai_meta_rows=ai_meta_rows)
        self.spreadsheets_api = FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


class AiMetaRowIdentityTests(unittest.TestCase):
    def test_ai_meta_append_failure_is_not_swallowed(self):
        with patch(
            "email_automation.ai_processing._execute_with_retry",
            side_effect=RuntimeError("AI_META append failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "AI_META append failed"):
                _append_ai_meta(
                    FakeSheets(),
                    "sheet-1",
                    3,
                    "Total SF",
                    "10000",
                    row_anchor="4402 Rex Rd",
                    ensure_tab=False,
                )

    def test_empty_proposal_returns_before_sheets_io(self):
        with patch("email_automation.ai_processing._sheets_client") as sheets_client:
            result = apply_proposal_to_sheet(
                "uid-1",
                "client-1",
                "sheet-1",
                ["Property Address"],
                3,
                ["4402 Rex Rd"],
                {"updates": []},
            )

        sheets_client.assert_not_called()
        self.assertEqual([], result["applied"])
        self.assertEqual("no-updates", result["skipped"][0]["reason"])

    def test_multi_field_apply_reads_ai_meta_only_once(self):
        fake_sheets = FakeSheets(ai_meta_rows=[])
        header = ["Property Address", "Total SF", "Power"]
        proposal = {
            "updates": [
                {"column": "Total SF", "value": "10000", "confidence": 0.99},
                {"column": "Power", "value": "800A 3-phase", "confidence": 0.99},
            ]
        }

        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            apply_proposal_to_sheet(
                "uid-1",
                "client-1",
                "sheet-1",
                header,
                3,
                ["4402 Rex Rd", "", ""],
                proposal,
            )

        self.assertEqual(1, len(fake_sheets.spreadsheets_api.get_calls))
        self.assertEqual(1, len(fake_sheets.values_api.get_calls))

    def test_apply_rolls_back_value_when_meta_append_definitely_failed(self):
        fake_sheets = FakeSheets(ai_meta_rows=[])
        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.ai_processing._load_ai_meta_rows", side_effect=[[], []]), \
             patch("email_automation.ai_processing._append_ai_meta", side_effect=RuntimeError("append failed")):
            with self.assertRaisesRegex(RuntimeError, "append failed"):
                apply_proposal_to_sheet(
                    "uid-1",
                    "client-1",
                    "sheet-1",
                    ["Property Address", "Total SF"],
                    3,
                    ["4402 Rex Rd", ""],
                    {"updates": [{
                        "column": "Total SF",
                        "value": "10000",
                        "confidence": 0.99,
                    }]},
                )

        self.assertEqual(2, len(fake_sheets.values_api.batch_update_calls))
        rollback = fake_sheets.values_api.batch_update_calls[1]["data"]
        self.assertEqual([[""]], rollback[0]["values"])

    def test_apply_accepts_ambiguous_meta_append_after_readback(self):
        fake_sheets = FakeSheets(ai_meta_rows=[])
        confirmed_meta = [[
            "rowNumber",
            "columnName",
            "last_ai_value",
            "last_ai_write_iso",
            "human_override",
            "rowAnchor",
        ], ["3", "Total SF", "10000", "2026-07-14T00:00:00Z", "False", "4402 Rex Rd"]]
        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.ai_processing._load_ai_meta_rows", side_effect=[[], confirmed_meta]), \
             patch("email_automation.ai_processing._append_ai_meta", side_effect=RuntimeError("response lost")), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid-1",
                "client-1",
                "sheet-1",
                ["Property Address", "Total SF"],
                3,
                ["4402 Rex Rd", ""],
                {"updates": [{
                    "column": "Total SF",
                    "value": "10000",
                    "confidence": 0.99,
                }]},
            )

        self.assertEqual("10000", result["applied"][0]["newValue"])
        self.assertEqual(1, len(fake_sheets.values_api.batch_update_calls))

    def test_apply_rolls_back_current_value_when_meta_readback_is_unavailable(self):
        fake_sheets = FakeSheets(ai_meta_rows=[])
        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch(
                 "email_automation.ai_processing._load_ai_meta_rows",
                 side_effect=[[], RuntimeError("readback unavailable")],
             ), \
             patch("email_automation.ai_processing._append_ai_meta", side_effect=RuntimeError("response lost")):
            with self.assertRaisesRegex(RuntimeError, "could not be reconciled"):
                apply_proposal_to_sheet(
                    "uid-1",
                    "client-1",
                    "sheet-1",
                    ["Property Address", "Total SF"],
                    3,
                    ["4402 Rex Rd", ""],
                    {"updates": [{
                        "column": "Total SF",
                        "value": "10000",
                        "confidence": 0.99,
                    }]},
                )

        self.assertEqual(2, len(fake_sheets.values_api.batch_update_calls))
        rollback = fake_sheets.values_api.batch_update_calls[1]["data"]
        self.assertEqual([[""]], rollback[0]["values"])

    def test_existing_ai_meta_tab_is_hidden_when_backend_touches_it(self):
        fake_sheets = FakeSheets()

        _ensure_ai_meta_tab(fake_sheets, "sheet-1")

        self.assertEqual(1, len(fake_sheets.spreadsheets_api.batch_update_calls))
        self.assertEqual(
            {
                "requests": [{
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": 1,
                            "hidden": True,
                        },
                        "fields": "hidden",
                    }
                }]
            },
            fake_sheets.spreadsheets_api.batch_update_calls[0],
        )

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

    def test_anchorless_ai_meta_does_not_block_blank_current_row_update(self):
        fake_sheets = FakeSheets(ai_meta_rows=[
            [
                "4",
                "Rent/SF/Yr",
                "16.20",
                "2026-06-01T00:00:00Z",
                "False",
                "",
            ],
        ])
        header = ["Property Address", "City", "Rent/SF/Yr"]
        current_row = ["951 E FM 646", "League City", ""]
        proposal = {
            "updates": [
                {
                    "column": "Rent/SF/Yr",
                    "value": "16.20",
                    "confidence": 0.92,
                    "reason": "Broker confirmed modified gross rent.",
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
                4,
                current_row,
                proposal,
            )

        self.assertEqual([], result["skipped"])
        self.assertEqual("Rent/SF/Yr", result["applied"][0]["column"])
        self.assertEqual("16.20", result["rowSnapshotAfter"]["Rent/SF/Yr"])

    def test_applied_result_includes_row_snapshot_evidence_for_reports(self):
        fake_sheets = FakeSheets()
        header = ["Property Address", "City", "Total SF", "Power"]
        current_row = ["777 Alternative Logistics Dr", "Mesa", "", ""]
        proposal = {
            "updates": [
                {
                    "column": "Total SF",
                    "value": "18,500",
                    "confidence": 0.95,
                    "reason": "Broker confirmed size.",
                },
                {
                    "column": "Power",
                    "value": "800A 3-phase",
                    "confidence": 0.9,
                    "reason": "Broker confirmed service.",
                },
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
                6,
                current_row,
                proposal,
            )

        self.assertEqual("777 Alternative Logistics Dr, Mesa", result["targetAnchor"])
        self.assertEqual(
            {
                "Property Address": "777 Alternative Logistics Dr",
                "City": "Mesa",
                "Total SF": "",
                "Power": "",
            },
            result["rowSnapshotBefore"],
        )
        self.assertEqual("18,500", result["rowSnapshotAfter"]["Total SF"])
        self.assertEqual("800A 3-phase", result["rowSnapshotAfter"]["Power"])


class PlaceholderValueGuardTests(unittest.TestCase):
    """apply_proposal_to_sheet must never write junk placeholder values (TBD, N/A)
    into a cell — including an EMPTY cell. The prior guard only screened placeholders
    in the *existing* value, so a placeholder *proposed* value slipped into blank
    cells. Grounded in live breaks E1 (TBD -> empty Power) and E2 (N/A -> empty Docks)."""

    def _apply(self, header, current_row, proposal, rownum=6):
        fake_sheets = FakeSheets()
        with patch("email_automation.ai_processing._sheets_client", return_value=fake_sheets), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid-1", "client-1", "sheet-1", header, rownum, current_row, proposal,
            )
        return result, fake_sheets

    def test_e1_tbd_placeholder_not_written_into_empty_power_cell(self):
        header = ["Property Address", "City", "Power"]
        current_row = ["8200 Trade Center Dr", "El Paso", ""]
        proposal = {"updates": [
            {"column": "Power", "value": "TBD", "confidence": 0.9,
             "reason": "Broker did not confirm power service."},
        ]}
        result, fake_sheets = self._apply(header, current_row, proposal)

        self.assertEqual([], result["applied"])
        self.assertEqual("", result["rowSnapshotAfter"]["Power"])
        # Assert it was skipped for the placeholder reason specifically — not some
        # unrelated skip (formula-column, low-confidence, etc.).
        power_skip = next(s for s in result["skipped"] if s.get("column") == "Power")
        self.assertEqual("placeholder-value", power_skip.get("reason"))
        # No sheet write, no AI_META append for a rejected placeholder.
        self.assertEqual(0, len(fake_sheets.values_api.batch_update_calls))
        self.assertEqual(0, len(fake_sheets.values_api.append_calls))

    def test_e2_na_placeholder_not_written_into_empty_docks_cell(self):
        header = ["Property Address", "City", "Docks"]
        current_row = ["8200 Trade Center Dr", "El Paso", ""]
        proposal = {"updates": [
            {"column": "Docks", "value": "N/A", "confidence": 0.9,
             "reason": "Broker did not confirm dock count."},
        ]}
        result, fake_sheets = self._apply(header, current_row, proposal)

        self.assertEqual([], result["applied"])
        self.assertEqual("", result["rowSnapshotAfter"]["Docks"])
        docks_skip = next(s for s in result["skipped"] if s.get("column") == "Docks")
        self.assertEqual("placeholder-value", docks_skip.get("reason"))
        self.assertEqual(0, len(fake_sheets.values_api.batch_update_calls))
        self.assertEqual(0, len(fake_sheets.values_api.append_calls))

    def test_real_value_still_written_into_empty_cell(self):
        # Guardrail: the placeholder screen must NOT block a legitimate value.
        header = ["Property Address", "City", "Power"]
        current_row = ["8200 Trade Center Dr", "El Paso", ""]
        proposal = {"updates": [
            {"column": "Power", "value": "800A 3-phase", "confidence": 0.9,
             "reason": "Broker confirmed service."},
        ]}
        result, _ = self._apply(header, current_row, proposal)
        self.assertEqual("800A 3-phase", result["rowSnapshotAfter"]["Power"])
        self.assertEqual("Power", result["applied"][0]["column"])


if __name__ == "__main__":
    unittest.main()
