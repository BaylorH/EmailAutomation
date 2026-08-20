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

class CorpusFalsePositiveTests(unittest.TestCase):
    """A real message this rule would have written a WRONG zero into the sheet for.

    Found by replaying every stored inbound through the rule rather than by
    inventing cases. A broker wrote:

        "No rail. No separate loading docks beyond the drive-ins."

    That sentence says drive-ins EXIST -- the negation belongs to the loading
    docks, and "beyond the drive-ins" names them as present. Writing zero there is
    the same silent corruption class as putting the rent figure in the operating
    expenses column: the sheet reads as answered, the number is false, and nobody
    finds out from the sheet.
    """

    def test_a_negation_about_docks_does_not_zero_the_drive_ins(self):
        self.assertIsNone(
            A._extract_drive_in_count_from_text(
                "Rate is $12.00/SF/year, NNN is $0.38/SF, 3-phase power. About 12% "
                "office. No rail. No separate loading docks beyond the drive-ins."
            )
        )

    def test_the_negation_must_belong_to_the_drive_in_itself(self):
        for text in [
            "No loading docks, but the drive-in doors are 14 feet.",
            "There is no rail service; drive-ins are available.",
        ]:
            with self.subTest(text=text[:44]):
                self.assertNotEqual(A._extract_drive_in_count_from_text(text), "0")

    def test_every_genuine_corpus_absence_still_records_zero(self):
        """The eight real messages where zero is the right answer."""
        for text in [
            "No drive in door. 1 loading dock. The unit is 7753 sf.",
            "The only space with enough square footage has dock-high loading, "
            "no drive-in doors.",
            "You can ramp one of the 2 loading docks. There's no existing drive-in.",
            "There are no grade-level drive-ins, 8 dock-high doors, 30-foot clear height.",
            "There are no drive-in doors (zero). There is one dock-high door.",
            "It currently has zero grade-level or drive-in doors.",
            "The current condition is zero drive-ins.",
        ]:
            with self.subTest(text=text[:44]):
                self.assertEqual(A._extract_drive_in_count_from_text(text), "0")


if __name__ == "__main__":
    unittest.main()
