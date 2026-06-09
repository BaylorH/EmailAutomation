import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

from email_automation.sheet_operations import _find_row_by_anchor


class FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeValues:
    def __init__(self, rows_by_range):
        self.rows_by_range = rows_by_range

    def get(self, spreadsheetId=None, range=None, **kwargs):
        return FakeRequest({"values": self.rows_by_range.get(range, [])})


class FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class FakeSheets:
    def __init__(self, rows_by_range):
        self._values = FakeValues(rows_by_range)
        self._spreadsheets = FakeSpreadsheets(self._values)

    def spreadsheets(self):
        return self._spreadsheets


class FakeThreadSnapshot:
    exists = True

    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class FakeThreadDocument:
    def __init__(self, data):
        self.data = data

    def get(self):
        return FakeThreadSnapshot(self.data)


class FakeThreadsCollection:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def document(self, thread_id):
        return FakeThreadDocument(self.thread_data[thread_id])


class FakeUserDocument:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def collection(self, name):
        if name != "threads":
            raise AssertionError(f"unexpected collection {name}")
        return FakeThreadsCollection(self.thread_data)


class FakeUsersCollection:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def document(self, user_id):
        return FakeUserDocument(self.thread_data)


class FakeFirestore:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def collection(self, name):
        if name != "users":
            raise AssertionError(f"unexpected root collection {name}")
        return FakeUsersCollection(self.thread_data)


class SheetRowAnchorSafetyTests(unittest.TestCase):
    def test_stale_row_uses_subject_address_before_duplicate_email_fallback(self):
        header = ["Property Address", "City", "Email"]
        rows_by_range = {
            "Sheet1!7:7": [["505 Wrong Row", "North Las Vegas", "bp21harrison@gmail.com"]],
            "Sheet1!A2:ZZZ": [
                header,
                ["930 Tour Loop", "Las Vegas", "bp21harrison@gmail.com"],
                ["970 Complete Specs Way", "North Las Vegas", "bp21harrison@gmail.com"],
            ],
        }
        thread_data = {
            "thread-970": {
                "subject": "Re: 970 Complete Specs Way dashboard-bp21-20260601T230836Z, North Las Vegas",
                "rowNumber": 7,
            }
        }

        with patch("email_automation.sheet_operations._fs", FakeFirestore(thread_data)):
            rownum, rowvals = _find_row_by_anchor(
                "uid-1",
                "thread-970",
                FakeSheets(rows_by_range),
                "sheet-1",
                "Sheet1",
                header,
                "bp21harrison@gmail.com",
            )

        self.assertEqual(4, rownum)
        self.assertEqual("970 Complete Specs Way", rowvals[0])

    def test_stale_row_with_unknown_subject_does_not_pick_duplicate_email_row(self):
        header = ["Property Address", "City", "Email"]
        rows_by_range = {
            "Sheet1!7:7": [["505 Wrong Row", "North Las Vegas", "bp21harrison@gmail.com"]],
            "Sheet1!A2:ZZZ": [
                header,
                ["930 Tour Loop", "Las Vegas", "bp21harrison@gmail.com"],
                ["970 Complete Specs Way", "North Las Vegas", "bp21harrison@gmail.com"],
            ],
        }
        thread_data = {
            "thread-unknown": {
                "subject": "Re: 999 Unknown Proof Way, North Las Vegas",
                "rowNumber": 7,
            }
        }

        with patch("email_automation.sheet_operations._fs", FakeFirestore(thread_data)):
            rownum, rowvals = _find_row_by_anchor(
                "uid-1",
                "thread-unknown",
                FakeSheets(rows_by_range),
                "sheet-1",
                "Sheet1",
                header,
                "bp21harrison@gmail.com",
            )

        self.assertIsNone(rownum)
        self.assertIsNone(rowvals)


if __name__ == "__main__":
    unittest.main()
