import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import re
import unittest
from unittest.mock import patch

from email_automation import email as email_module


class FakeSheetsRequest:
    def __init__(self, values):
        self.values = values

    def execute(self):
        return {"values": self.values}


class FakeSheetsValues:
    """Datastore boundary: returns a DIFFERENT row body per requested A1 range.

    The production resolver reads a single row via range ``{tab}!{n}:{n}``.
    This fake keys its response off the row number parsed from that range, so
    the only way a resolution can return a neighbor's name is if the real code
    fetched (or leaked from) the wrong row.
    """

    def __init__(self, rows_by_number):
        self.rows_by_number = rows_by_number
        self.ranges = []

    def get(self, **kwargs):
        rng = kwargs.get("range", "")
        self.ranges.append((kwargs.get("spreadsheetId"), rng))
        m = re.search(r"!(\d+):\d+$", rng)
        row_number = int(m.group(1)) if m else None
        row = self.rows_by_number.get(row_number, [])
        return FakeSheetsRequest([row])


class FakeSheetsSpreadsheets:
    def __init__(self, rows_by_number):
        self.values_api = FakeSheetsValues(rows_by_number)

    def values(self):
        return self.values_api


class FakeSheetsClient:
    def __init__(self, rows_by_number):
        self.spreadsheets_api = FakeSheetsSpreadsheets(rows_by_number)

    def spreadsheets(self):
        return self.spreadsheets_api


class CoreNameResolutionWrongRecipientTests(unittest.TestCase):
    """Rubric cell: feature=core.name_resolution, class=wrong_recipient.

    Proves the REAL production resolver
    ``email._resolve_campaign_launch_contact_name_from_sheet`` binds the
    contact name to the INTENDED campaign row only, never bleeding a
    neighbor row's contact into the greeting. A wrong-recipient failure here
    would mean row 12's send is personalized with row 13's name.
    """

    INTENDED_ROW = 12
    NEIGHBOR_ROW = 13
    INTENDED_NAME = "Avery Brooks"
    NEIGHBOR_NAME = "Blake Turner"

    def _resolve(self, row_number):
        # Two adjacent rows with genuinely DIFFERENT contacts. Only patch the
        # Sheets datastore boundary; the resolver under test runs for real.
        rows_by_number = {
            self.INTENDED_ROW: [self.INTENDED_NAME, "intended@example.com", "100 Main St"],
            self.NEIGHBOR_ROW: [self.NEIGHBOR_NAME, "neighbor@example.com", "101 Main St"],
        }
        fake_sheets = FakeSheetsClient(rows_by_number)
        data = {
            "clientId": "client-1",
            "rowNumber": row_number,
            "source": "dashboard_new_campaign",
            "actionType": "campaign_creation",
        }
        with patch.object(email_module, "_get_sheet_id_or_fail", return_value="sheet-1"), \
             patch.object(email_module, "_sheets_client", return_value=fake_sheets), \
             patch.object(email_module, "_get_first_tab_title", return_value="Campaign"), \
             patch.object(
                 email_module,
                 "_read_header_row2",
                 return_value=["Leasing Contact", "Email", "Address"],
             ):
            name = email_module._resolve_campaign_launch_contact_name_from_sheet(
                "uid-1", data
            )
        return name, fake_sheets

    def test_resolution_binds_to_intended_row_not_neighbor(self):
        name, fake_sheets = self._resolve(self.INTENDED_ROW)

        # The resolver produced the intended row's real contact name.
        self.assertEqual(self.INTENDED_NAME, name)
        # Discriminating negative control: it must NOT be the adjacent row's
        # contact. If the resolver read/leaked the neighbor row, this fails.
        self.assertNotEqual(self.NEIGHBOR_NAME, name)
        # The real read path targeted exactly the intended row range, never the
        # neighbor's — proving row selection, not coincidence of identical data.
        read_ranges = [rng for (_sid, rng) in fake_sheets.spreadsheets_api.values_api.ranges]
        self.assertIn("Campaign!12:12", read_ranges)
        self.assertNotIn("Campaign!13:13", read_ranges)

    def test_negative_control_neighbor_row_yields_neighbor_name(self):
        # Proves the two rows are genuinely distinct and reachable: resolving
        # the neighbor row returns the neighbor's name (not the intended one).
        # Without this, the intended-row assertion could pass on identical data.
        name, _ = self._resolve(self.NEIGHBOR_ROW)
        self.assertEqual(self.NEIGHBOR_NAME, name)
        self.assertNotEqual(self.INTENDED_NAME, name)


if __name__ == "__main__":
    unittest.main()
