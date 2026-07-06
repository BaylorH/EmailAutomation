import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest

from email_automation.sheet_operations import (
    _apply_gross_rent_formula_for_row,
    _build_gross_rent_formula_for_row,
)


class _FakeRequest:
    """Mimics a prepared Sheets API request; records the write on execute()."""

    def __init__(self, cell_store, call_log, range_, values):
        self._cell_store = cell_store
        self._call_log = call_log
        self._range = range_
        self._values = values

    def execute(self):
        # Google Sheets values().update is a SET on the target range, not an
        # append. Model that faithfully: overwrite whatever is at the range.
        self._call_log.append((self._range, self._values))
        self._cell_store[self._range] = self._values
        return {"updatedCells": 1}


class _FakeValues:
    def __init__(self, cell_store, call_log):
        self._cell_store = cell_store
        self._call_log = call_log

    def update(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
        return _FakeRequest(self._cell_store, self._call_log, range, body["values"])


class _FakeSpreadsheets:
    def __init__(self, cell_store, call_log):
        self._values = _FakeValues(cell_store, call_log)

    def values(self):
        return self._values


class _FakeSheets:
    def __init__(self):
        self.cell_store = {}
        self.call_log = []
        self._spreadsheets = _FakeSpreadsheets(self.cell_store, self.call_log)

    def spreadsheets(self):
        return self._spreadsheets


class CoreSheetUpdateDuplicateRetryTests(unittest.TestCase):
    def test_reapplying_same_proposal_is_idempotent_no_double_formula(self):
        """Applying the same gross-rent formula proposal twice must land the
        identical formula in the identical cell, never a doubled/corrupted one.

        This exercises the REAL production function
        ``_apply_gross_rent_formula_for_row`` (an owner module of
        core.sheet_update) against a fake that models Google Sheets'
        ``values().update`` SET semantics. A duplicate retry of the same
        proposal is the duplicate_retry fixture class; corruption here would
        trip the productionGate stop condition
        ``formula_overwritten_without_user_choice``.
        """
        header = ["Property Address", "Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Gross Rent"]
        rownum = 4

        # The proposal the pipeline would apply, computed deterministically.
        gross_col, expected_formula = _build_gross_rent_formula_for_row(header, rownum)
        self.assertEqual("E", gross_col)
        expected_range = f"Sheet1!{gross_col}{rownum}"

        sheets = _FakeSheets()

        # First application of the proposal.
        first = _apply_gross_rent_formula_for_row(sheets, "sheet-1", "Sheet1", header, rownum)
        self.assertTrue(first)
        state_after_first = dict(sheets.cell_store)

        # Duplicate retry: apply the SAME proposal a second time.
        second = _apply_gross_rent_formula_for_row(sheets, "sheet-1", "Sheet1", header, rownum)
        self.assertTrue(second)

        # Exactly one gross-rent cell is touched, and its final content is a
        # SINGLE formula equal to the built proposal -- not doubled/appended.
        self.assertEqual([expected_range], list(sheets.cell_store.keys()))
        self.assertEqual([[expected_formula]], sheets.cell_store[expected_range])

        # Idempotence: the sheet state is byte-identical before and after the
        # duplicate retry -- no formula/highlight corruption accrued.
        self.assertEqual(state_after_first, sheets.cell_store)

        # Defense in depth: the stored formula is one formula, not two
        # concatenated (a classic double-apply corruption signature).
        stored_formula = sheets.cell_store[expected_range][0][0]
        self.assertTrue(stored_formula.startswith("=IF("))
        self.assertEqual(1, stored_formula.count("=IF(OR("))


if __name__ == "__main__":
    unittest.main()
