import os
os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/baylorharrison/Documents/GitHub/EmailAutomation/service-account.json",
)

import unittest
from unittest.mock import patch

from email_automation.sheet_operations import _find_row_by_anchor


# --- Minimal fakes for the Google Sheets client -------------------------------
class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeValues:
    def __init__(self, rows_by_range):
        self.rows_by_range = rows_by_range

    def get(self, spreadsheetId=None, range=None, **kwargs):
        return _FakeRequest({"values": self.rows_by_range.get(range, [])})


class _FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class _FakeSheets:
    def __init__(self, rows_by_range):
        self._values = _FakeValues(rows_by_range)
        self._spreadsheets = _FakeSpreadsheets(self._values)

    def spreadsheets(self):
        return self._spreadsheets


# --- Minimal fakes for Firestore thread lookup --------------------------------
class _FakeSnapshot:
    exists = True

    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeThreadDoc:
    def __init__(self, data):
        self.data = data

    def get(self):
        return _FakeSnapshot(self.data)


class _FakeThreads:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def document(self, thread_id):
        return _FakeThreadDoc(self.thread_data[thread_id])


class _FakeUserDoc:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def collection(self, name):
        assert name == "threads"
        return _FakeThreads(self.thread_data)


class _FakeUsers:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def document(self, user_id):
        return _FakeUserDoc(self.thread_data)


class _FakeFirestore:
    def __init__(self, thread_data):
        self.thread_data = thread_data

    def collection(self, name):
        assert name == "users"
        return _FakeUsers(self.thread_data)


class CoreSheetUpdateWrongRecipientTest(unittest.TestCase):
    """core.sheet_update / wrong_recipient.

    Proves the REAL production finder ``_find_row_by_anchor`` will not resolve an
    update onto the wrong recipient's row when the sheet has been moved so that
    the thread's stored ``rowNumber`` now points at a *different* broker/address.
    The finder must re-anchor by the thread subject and return the row that
    actually belongs to this thread — the correct recipient — instead of the
    stale (now-wrong) row the pointer still names.
    """

    def test_update_reanchors_to_correct_row_not_stale_wrong_recipient_row(self):
        header = ["Property Address", "City", "Email"]
        # Rows moved: stored rowNumber 5 now physically holds a DIFFERENT
        # recipient (a different address AND a different broker email) than the
        # one this thread is about. The correct thread row is now row 3.
        rows_by_range = {
            # Snapshot of the stale stored row (row 5) — the wrong recipient.
            "Sheet1!5:5": [["111 Someone Else Ave", "Henderson", "other-broker@example.com"]],
            # Full-sheet scan used by the subject anchor.
            "Sheet1!A2:ZZZ": [
                header,
                ["808 Correct Deal Rd", "Las Vegas", "target-broker@example.com"],
                ["111 Someone Else Ave", "Henderson", "other-broker@example.com"],
            ],
        }
        thread_data = {
            "thread-808": {
                "subject": "Re: 808 Correct Deal Rd tok-20260701T000000Z, Las Vegas",
                "rowNumber": 5,  # stale pointer after the move
            }
        }

        with patch("email_automation.sheet_operations._fs", _FakeFirestore(thread_data)):
            rownum, rowvals = _find_row_by_anchor(
                "uid-1",
                "thread-808",
                _FakeSheets(rows_by_range),
                "sheet-1",
                "Sheet1",
                header,
                "target-broker@example.com",
            )

        # Must land on the correct thread row (row 3, "808 Correct Deal Rd"),
        # NOT the stale row 5 that now belongs to the wrong recipient.
        self.assertEqual(3, rownum)
        self.assertEqual("808 Correct Deal Rd", rowvals[0])
        self.assertEqual("target-broker@example.com", rowvals[2])
        # Explicitly guard against writing to the wrong recipient's row.
        self.assertNotEqual(5, rownum)
        self.assertNotIn("111 Someone Else Ave", rowvals)


if __name__ == "__main__":
    unittest.main()
