"""A stated absence of drive-in doors is an ANSWER -- found in real customer traffic.

Companion to test_definitive_none_answers. Same live thread, same failure shape, a
different mechanism: the broker wrote "You can ramp one of the 2 loading docks.
There's no existing drive-in." and no zero-detection pattern matched that phrasing,
so nothing was ever proposed for the Drive Ins column. An empty cell reads as
missing, and the system asked him for it thirty seconds later, alongside the
operating expenses he had also just answered.

A count he states wins; an absence he states records zero; silence records nothing.
"""
import os
import sys
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email_automation.ai_processing as A  # noqa: E402

NO_DRIVE_IN = "You can ramp one of the 2 loading docks. There's no existing drive-in."


class NoDriveInRecordsZeroTests(unittest.TestCase):
    def test_the_live_phrasing_is_detected_as_zero(self):
        self.assertEqual(A._extract_drive_in_count_from_text(NO_DRIVE_IN), "0")

    def test_no_drive_in_phrase_family(self):
        for text in [
            "There's no existing drive-in.",
            "There is no drive-in door.",
            "No drive-in doors at this building.",
            "The suite has no grade-level access.",
            "There are no grade level doors.",
        ]:
            with self.subTest(text=text):
                self.assertEqual(A._extract_drive_in_count_from_text(text), "0")

    def test_a_real_count_still_wins(self):
        self.assertEqual(A._extract_drive_in_count_from_text("There are 3 drive-in doors."), "3")

    def test_silence_about_drive_ins_records_nothing(self):
        self.assertIsNone(A._extract_drive_in_count_from_text("The space is 22,000 SF."))

    def test_a_dock_statement_is_not_a_drive_in_zero(self):
        """He said no DRIVE-IN while describing two docks; docks must be untouched."""
        self.assertIsNone(A._extract_drive_in_count_from_text("There are no loading docks."))

if __name__ == "__main__":
    unittest.main()
