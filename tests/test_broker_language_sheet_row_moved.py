"""
Pressure test for EVENT: sheet_row_moved

Deterministic guard under test:
    email_automation.sheet_operations._find_row_by_anchor
    (called from processing.py:2918 to decide WHICH sheet row a broker
     reply's extracted data is written into)

Supporting helpers exercised:
    _row_matches_subject_anchor, _find_row_by_subject_anchor,
    _find_unique_row_by_email

Safety property (stopIf):
  * updates must NOT land on the wrong property row after a sort/insert
  * a legitimate anchored update must NOT be blocked just because the
    display row number changed

We drive the REAL function. Only external boundaries are faked:
  * Firestore  -> email_automation.sheet_operations._fs (patched)
  * Sheets API -> a fake `sheets` object passed as an argument

NO real Firestore / Sheets / Graph calls happen.
"""

import os
import re
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.sheet_operations import _find_row_by_anchor


HEADER = ["Property Address", "City", "Email", "Status"]


# --------------------------------------------------------------------------
# Fake Sheets API backed by an in-memory grid.
# grid: {sheet_rownum(int, data starts at 3): [cell, cell, ...]}
# The header lives at row 2 and is returned as values[0] for A2:ZZZ scans.
# --------------------------------------------------------------------------
class _FakeExec:
    def __init__(self, values):
        self._values = values

    def execute(self):
        return {"values": self._values}


class _FakeValues:
    def __init__(self, grid, header):
        self._grid = grid
        self._header = header

    def get(self, spreadsheetId=None, range=None):  # noqa: A002 (mirror google api kw)
        rng = range.split("!", 1)[1]
        if rng.upper().startswith("A2"):
            # Header row (row 2) then all data rows in row order.
            ordered = [self._grid[r] for r in sorted(self._grid)]
            return _FakeExec([list(self._header)] + ordered)
        m = re.match(r"(\d+):(\d+)$", rng)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            out = [self._grid[r] for r in range_inclusive(start, end) if r in self._grid]
            return _FakeExec(out)
        return _FakeExec([])


def range_inclusive(a, b):
    return list(range(a, b + 1))


class _FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values


class FakeSheets:
    def __init__(self, grid, header=HEADER):
        self._s = _FakeSpreadsheets(_FakeValues(grid, header))

    def spreadsheets(self):
        return self._s


# --------------------------------------------------------------------------
# Fake Firestore _fs supporting:
#   _fs.collection("users").document(uid).collection("threads").document(tid).get()
# --------------------------------------------------------------------------
def make_fs(thread_map):
    def make_ref(tid):
        data = thread_map.get(tid)
        doc = MagicMock()
        doc.exists = data is not None
        doc.to_dict.return_value = data
        ref = MagicMock()
        ref.get.return_value = doc
        return ref

    threads_col = MagicMock()
    threads_col.document.side_effect = make_ref
    user_doc = MagicMock()
    user_doc.collection.return_value = threads_col
    users_col = MagicMock()
    users_col.document.return_value = user_doc
    fs = MagicMock()
    fs.collection.return_value = users_col
    return fs


def run_anchor(thread_data, grid, header=HEADER, fallback_email="", uid="u1", tid="t1"):
    """Drive the real guard with fakes; returns (rownum, rowvals)."""
    fs = make_fs({tid: thread_data})
    sheets = FakeSheets(grid, header)
    with patch("email_automation.sheet_operations._fs", fs):
        return _find_row_by_anchor(uid, tid, sheets, "SHEET_ID", "Sheet1", header, fallback_email)


# --------------------------------------------------------------------------
# 15+ subject phrasing variations that all denote the SAME property:
#   "742 Evergreen Terrace, Springfield"
# In every case the broker has SORTED/INSERTED so the stored rowNumber (3)
# now holds a DIFFERENT property, and the real property has moved to row 5.
# A correct guard must re-anchor to row 5 and never return row 3.
# --------------------------------------------------------------------------
PROPERTY_SUBJECTS = [
    "742 Evergreen Terrace, Springfield",                       # clean
    "RE: 742 Evergreen Terrace, Springfield",                   # reply prefix
    "Re: Re: 742 Evergreen Terrace, Springfield",               # nested reply
    "FWD: 742 Evergreen Terrace, Springfield",                  # forward prefix
    "Fwd: Re: 742 Evergreen Terrace, Springfield",              # mixed prefixes
    "742 EVERGREEN TERRACE, SPRINGFIELD",                       # ALL CAPS
    "742 evergreen terrace, springfield",                       # lowercase
    "  742   Evergreen Terrace ,   Springfield  ",              # messy whitespace
    "742 Evergreen Terrace, Springfield [CAMPAIGN-9f2a]",       # trailing bracket run-tag
    "Re: 742 Evergreen Terrace, Springfield [proof-run-7]",     # prefix + bracket tag
    "742 Evergreen Terrace, Springfield, IL",                   # extra region token
    "FW: 742 Evergreen Terrace, Springfield",                   # short forward prefix
    "re: 742 evergreen terrace, springfield",                   # lowercase prefix
    "742 Evergreen Terrace,Springfield",                        # no space after comma
    "RE:742 Evergreen Terrace, Springfield",                    # no space after prefix colon
    "Re: Fwd: Re: 742 EVERGREEN TERRACE, springfield [x]",      # kitchen sink
]


class SheetRowMovedGuard(unittest.TestCase):

    # ---- Grid used by the phrasing sweep -------------------------------
    def _moved_grid(self):
        # Stored rowNumber=3 now holds a DIFFERENT property (broker sorted).
        return {
            3: ["100 Nowhere Rd", "Nowhere", "someoneelse@example.com", "active"],
            4: ["55 Filler Ave", "Springfield", "filler@example.com", "active"],
            5: ["742 Evergreen Terrace", "Springfield", "broker@example.com", "active"],
        }

    # =====================================================================
    # REAL-THREAT SWEEP: after a sort, the stored row is stale. For every
    # phrasing that parses to the address, the guard must land on row 5
    # (the real property) and NEVER on row 3 (the wrong property).
    # =====================================================================
    def test_phrasings_reanchor_to_correct_row_after_sort(self):
        misfires = []
        for subj in PROPERTY_SUBJECTS:
            thread = {"subject": subj, "rowNumber": 3}  # stale stored row
            rownum, _ = run_anchor(
                thread, self._moved_grid(), fallback_email="broker@example.com"
            )
            if rownum != 5:
                misfires.append((subj, rownum))
        self.assertEqual(
            misfires, [],
            f"Guard landed on the WRONG property (expected row 5) for: {misfires}",
        )

    # =====================================================================
    # BUG 1 (false negative, HIGH): subject has NO parseable address.
    # _row_matches_subject_anchor returns True for ANY row when addr=="",
    # so the stale stored rowNumber is blindly trusted after a sort/insert
    # and the update lands on the wrong property. Email uniquely identifies
    # the correct row (5), so the safe answer is row 5 (or refuse), NOT 3.
    # =====================================================================
    def test_empty_subject_does_not_trust_stale_stored_row(self):
        thread = {"subject": "Re: Following up on our conversation", "rowNumber": 3}
        grid = self._moved_grid()
        rownum, rowvals = run_anchor(thread, grid, fallback_email="broker@example.com")
        self.assertNotEqual(
            rownum, 3,
            "SAFETY: empty-address subject caused the stale stored row 3 "
            "(a DIFFERENT property) to be trusted after a sort -> update lands "
            "on the wrong property.",
        )
        # The correct property is uniquely resolvable by broker email at row 5.
        self.assertEqual(rownum, 5,
                         "Guard should have re-anchored to the correct property (row 5).")

    def test_blank_subject_does_not_trust_stale_stored_row(self):
        thread = {"subject": "", "rowNumber": 3}
        grid = self._moved_grid()
        rownum, _ = run_anchor(thread, grid, fallback_email="broker@example.com")
        self.assertNotEqual(
            rownum, 3,
            "SAFETY: blank subject trusted stale stored row 3 (wrong property).",
        )
        self.assertEqual(rownum, 5)

    # =====================================================================
    # BUG 2 (false negative, HIGH): address SUBSTRING collision.
    # Thread is for "110 Main St"; after a sort, "10 Main St" sits at an
    # earlier row. _row_matches_subject_anchor accepts either containment
    # direction, so "10 main st" matches the "110 main st" thread and the
    # subject scan returns the WRONG earlier row even though the exact
    # property exists lower in the sheet.
    # =====================================================================
    def test_substring_address_collision_lands_on_wrong_row(self):
        thread = {"subject": "110 Main St, Austin"}  # no stored rowNumber
        grid = {
            3: ["10 Main St", "Austin", "other@example.com", "active"],   # decoy, substring
            5: ["110 Main St", "Austin", "target@example.com", "active"], # real property
        }
        rownum, rowvals = run_anchor(thread, grid, fallback_email="target@example.com")
        self.assertEqual(
            rownum, 5,
            "SAFETY: '10 Main St' substring-matched the '110 Main St' thread; "
            f"update would land on the wrong property (got row {rownum}).",
        )

    # A tighter variant: the ONLY row present is the decoy substring; the
    # real property is not in the sheet at all. The guard must NOT claim a
    # match (returning the decoy = writing to the wrong property).
    def test_substring_only_decoy_present_must_not_match(self):
        thread = {"subject": "110 Main St, Austin"}
        grid = {
            3: ["10 Main St", "Austin", "decoy@example.com", "active"],
        }
        rownum, _ = run_anchor(thread, grid, fallback_email="")
        self.assertIsNone(
            rownum,
            "SAFETY: guard matched decoy '10 Main St' for a '110 Main St' thread "
            "(no fallback email) -> update lands on the wrong property.",
        )

    # =====================================================================
    # NEAR-MISS 1 (must NOT fire): display row moved but the durable
    # property anchor still matches. Guard should proceed and return the
    # anchored row, not refuse.
    # =====================================================================
    def test_near_miss_durable_anchor_still_matches(self):
        thread = {"subject": "742 Evergreen Terrace, Springfield", "rowNumber": 5}
        grid = {
            3: ["100 Nowhere Rd", "Nowhere", "x@example.com", "active"],
            5: ["742 Evergreen Terrace", "Springfield", "broker@example.com", "active"],
        }
        rownum, _ = run_anchor(thread, grid, fallback_email="broker@example.com")
        self.assertEqual(
            rownum, 5,
            "FALSE POSITIVE: durable anchor matched but guard did not confirm row 5.",
        )

    # =====================================================================
    # NEAR-MISS 2 (must NOT fire): a formula/other column changed position
    # after campaign creation. Address is resolved by header NAME, so a
    # reordered header must still anchor correctly (no false block).
    # =====================================================================
    def test_near_miss_column_reordered_still_anchors(self):
        header = ["Status", "City", "Gross Rent", "Property Address", "Email"]
        thread = {"subject": "742 Evergreen Terrace, Springfield", "rowNumber": 4}
        grid = {
            3: ["active", "Nowhere", "=X", "100 Nowhere Rd", "x@example.com"],
            4: ["active", "Springfield", "=Y", "742 Evergreen Terrace", "broker@example.com"],
        }
        rownum, _ = run_anchor(
            thread, grid, header=header, fallback_email="broker@example.com"
        )
        self.assertEqual(
            rownum, 4,
            "FALSE POSITIVE: moving a non-anchor column broke address anchoring.",
        )

    # =====================================================================
    # CONTROL: broker inserts rows ABOVE a pending action WITHOUT the app
    # observing the insert (stored rowNumber never synced). Stale stored
    # row 3 now holds a different property; subject re-anchors to row 6.
    # =====================================================================
    def test_manual_insert_above_reanchors_by_subject(self):
        thread = {"subject": "742 Evergreen Terrace, Springfield", "rowNumber": 3}
        grid = {
            3: ["NEW INSERTED ROW A", "Springfield", "newa@example.com", "active"],
            4: ["NEW INSERTED ROW B", "Springfield", "newb@example.com", "active"],
            5: ["NEW INSERTED ROW C", "Springfield", "newc@example.com", "active"],
            6: ["742 Evergreen Terrace", "Springfield", "broker@example.com", "active"],
        }
        rownum, _ = run_anchor(thread, grid, fallback_email="broker@example.com")
        self.assertEqual(
            rownum, 6,
            f"After manual insert-above, guard should re-anchor to row 6 (got {rownum}).",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
