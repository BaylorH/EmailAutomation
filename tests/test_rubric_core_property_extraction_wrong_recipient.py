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
# (apply_proposal_to_sheet + _read_ai_meta_row + get_row_anchor) runs for real.
# ---------------------------------------------------------------------------
class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeValues:
    def __init__(self, ai_meta_rows):
        self.batch_update_calls = []
        self.append_calls = []
        self.ai_meta_rows = ai_meta_rows

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
                    *self.ai_meta_rows,
                ]
            })
        return FakeRequest({"values": []})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return FakeRequest({})

    def append(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
        self.append_calls.append({"range": range, "body": body})
        return FakeRequest({})


class FakeSpreadsheets:
    def __init__(self, values):
        self._values = values
        self.batch_update_calls = []

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
        self.batch_update_calls.append(body)
        return FakeRequest({})


class FakeSheets:
    def __init__(self, ai_meta_rows):
        self.values_api = FakeValues(ai_meta_rows=ai_meta_rows)
        self.spreadsheets_api = FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


class CorePropertyExtractionWrongRecipientTests(unittest.TestCase):
    """core.property_extraction / wrong_recipient.

    A stale AI_META record can share the same physical rowNumber as the row the
    proposal now targets (the property that previously lived in that row, or a
    duplicated/retried write). If the backend guarded writes by rowNumber alone,
    a *neighbor's* AI_META record would silently govern the current property and
    misroute the extraction proposal — writing to / blocking the wrong recipient
    row. The anchor identity check (get_row_anchor + _read_ai_meta_row) exists to
    prevent exactly that. This test drives the real apply_proposal_to_sheet and
    proves the anchor — not the rowNumber — decides row identity, using a
    negative control that flips only the AI_META anchor.
    """

    # Header + row shared by both arms. Row 3 now holds "404 New Way".
    HEADER = ["Property Address", "City", "Total SF"]
    ROWNUM = 3
    CURRENT_ROW = ["404 New Way", "Dallas", "9,000"]  # human-set Total SF = 9,000

    # Proposal wants to raise Total SF to 6,000 with high confidence.
    PROPOSAL = {
        "updates": [
            {
                "column": "Total SF",
                "value": "6,000",
                "confidence": 0.95,
                "reason": "Broker confirmed the current property's square footage.",
            }
        ]
    }

    def _run(self, ai_meta_rows):
        fake_sheets = FakeSheets(ai_meta_rows=ai_meta_rows)
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
                self.PROPOSAL,
            )
        return fake_sheets, result

    def test_neighbor_ai_meta_does_not_hijack_current_rows_extraction_proposal(self):
        # --- MAIN CASE: AI_META row #3 belongs to a *neighbor* property that used
        # to sit in row 3 ("101 Old St, Dallas"). Its stale last_ai_value=10,000
        # does not match the current human value 9,000, so a rowNumber-only guard
        # would misfire "human-override" and block the write. The anchor guard must
        # recognize the record belongs to a different property and ignore it, so the
        # proposal reaches the *correct* row (404 New Way) and applies.
        neighbor_anchor_meta = [
            ["3", "Total SF", "10,000", "2026-06-01T00:00:00Z", "False", "101 Old St, Dallas"],
        ]
        fake_sheets, result = self._run(neighbor_anchor_meta)

        # Proposal targets the row identified by ITS anchor, not the neighbor's.
        self.assertEqual("404 New Way, Dallas", result["targetAnchor"])
        # The neighbor's stale AI_META did not block the write.
        self.assertEqual([], result["skipped"])
        self.assertEqual(1, len(result["applied"]))
        applied = result["applied"][0]
        self.assertEqual("Total SF", applied["column"])
        self.assertEqual("Sheet1!C3", applied["range"])
        self.assertEqual("6,000", applied["newValue"])
        self.assertEqual("6,000", result["rowSnapshotAfter"]["Total SF"])
        # And the real datastore write actually happened at C3 (the target row).
        self.assertEqual(1, len(fake_sheets.values_api.batch_update_calls))
        written = fake_sheets.values_api.batch_update_calls[0]["data"][0]
        self.assertEqual("Sheet1!C3", written["range"])
        self.assertEqual([["6,000"]], written["values"])

        # --- NEGATIVE CONTROL: identical inputs EXCEPT the AI_META anchor now
        # matches the current row (a genuine prior AI write on THIS property, since
        # human-corrected to 9,000). Now the record legitimately governs the row,
        # so the same proposal MUST be blocked as human-override. This proves the
        # main-case pass is caused by the anchor mismatch, not by the write being
        # unconditionally allowed.
        same_anchor_meta = [
            ["3", "Total SF", "10,000", "2026-06-01T00:00:00Z", "False", "404 New Way, Dallas"],
        ]
        _, control = self._run(same_anchor_meta)

        self.assertEqual("404 New Way, Dallas", control["targetAnchor"])
        self.assertEqual([], control["applied"])
        self.assertEqual(1, len(control["skipped"]))
        self.assertEqual("Total SF", control["skipped"][0]["column"])
        self.assertEqual("human-override", control["skipped"][0]["reason"])
        # Current human value is preserved; nothing overwrote the correct row.
        self.assertEqual("9,000", control["rowSnapshotAfter"]["Total SF"])


if __name__ == "__main__":
    unittest.main()
