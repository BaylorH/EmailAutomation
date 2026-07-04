"""
Combination stress deck: same_broker_multi_property_split_subject
(docs/release-safety/feature-gradebook.json -> combinationStressDecks)

Chained playbooks driven together as ONE scenario:
  * same_broker_multiple_properties  -- one broker inbox owns two rows; reply
      matching must never update all rows or the wrong row by email alone.
  * subject_drift_split_thread       -- broker changes the subject / replies on
      a different chain; the matcher must re-link only to the property the
      visible subject names.
  * row_move_during_pending_action   -- a sheet sort moves rows after launch
      while an action is pending; updates must follow a DURABLE anchor, never a
      stale display row number.
Crossed with the "one property complete and one partial" variant.

mustProve (deck):
  1. only intended row updates
  2. split thread is linked only when safe
  3. completed row does not receive follow-up for partial row

Real handlers under test (NO stubs of the logic itself):
  * email_automation.sheet_operations._find_row_by_anchor
        (production reply->row matcher; called at processing.py:3085)
  * email_automation.followup._followup_terminal_block_reason
        (production follow-up terminal guard; called at followup.py:721)

Only external boundaries are faked:
  * Firestore  -> email_automation.sheet_operations._fs (patched)
  * Sheets API -> an in-memory fake `sheets` object passed as an argument
  * Graph      -> never reached (no send path is invoked)
ZERO live sends, zero live sheet writes, zero live Firestore reads.

--------------------------------------------------------------------------
TDD note -- a real interaction bug this deck surfaced and this test locks in:
  Before the fix, _find_row_by_anchor step 1 trusted a STORED rowNumber on a
  token-PREFIX match. When the same broker owns "22 Oak Ave" (completed) and
  "22 Oak Ave North" (partial), a sheet sort could leave the partial thread's
  stored rowNumber pointing at the completed sibling ("22 Oak Ave" is a
  whole-token prefix of "22 Oak Ave North"). The single-row check accepted that
  prefix and the partial broker's reply was written onto the COMPLETED row --
  violating mustProve #1 (wrong row) and #3 (completed row absorbed partial
  data). The fix makes the stored-row check EXACT-only, forcing a prefix-only
  stale row through the exact-preferring full-sheet scan.
  test_row_move_reanchors_partial_reply_off_completed_sibling is the regression
  guard: it FAILS (returns the completed row 4) if that fix is reverted.
"""

import os
import re
import unittest
from unittest.mock import patch, MagicMock

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation.sheet_operations import _find_row_by_anchor
from email_automation.followup import _followup_terminal_block_reason


HEADER = ["Property Address", "City", "Email", "Status"]


# --------------------------------------------------------------------------
# Fake Sheets API backed by an in-memory grid.
#   grid: {absolute_row_number(int): [cell, cell, ...]}
#   Header is at row 2; data rows begin at row 3. An A2:ZZZ scan returns the
#   header row followed by every data row in row order; an "N:N" range returns
#   just that row (empty list when absent), matching Google's values.get shape.
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

    def get(self, spreadsheetId=None, range=None):  # noqa: A002 (mirror google kwarg)
        rng = range.split("!", 1)[1]
        if rng.upper().startswith("A2"):
            ordered = [self._grid[r] for r in sorted(self._grid)]
            return _FakeExec([list(self._header)] + ordered)
        m = re.match(r"(\d+):(\d+)$", rng)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            wanted = [n for n in _int_range(lo, hi) if n in self._grid]
            return _FakeExec([self._grid[n] for n in wanted])
        return _FakeExec([])


def _int_range(a, b):
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
# Fake Firestore _fs supporting the exact chain _find_row_by_anchor walks:
#   _fs.collection("users").document(uid).collection("threads").document(tid).get()
# --------------------------------------------------------------------------
def _make_fs(thread_map):
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


def _run_anchor(thread_data, grid, header=HEADER, fallback_email="", uid="u1", tid="t1"):
    """Drive the REAL reply->row matcher with only its boundaries faked."""
    fs = _make_fs({tid: thread_data})
    sheets = FakeSheets(grid, header)
    with patch("email_automation.sheet_operations._fs", fs):
        return _find_row_by_anchor(uid, tid, sheets, "SHEET_ID", "Sheet1", header, fallback_email)


# --------------------------------------------------------------------------
# The shared world for this deck.  ONE broker inbox (broker@acme.com) owns TWO
# properties in the same sheet:
#   * "22 Oak Ave"        (Austin) -- COMPLETED (a whole-token PREFIX of below)
#   * "22 Oak Ave North"  (Austin) -- PARTIAL, still collecting specs
# Row 3 holds an unrelated property from a different broker (a decoy that must
# never be selected).
# --------------------------------------------------------------------------
PARTIAL_ADDR = "22 Oak Ave North"
COMPLETED_ADDR = "22 Oak Ave"
BROKER = "broker@acme.com"

# Launch-time layout (before any broker sort).
LAUNCH_GRID = {
    3: ["900 Elm Blvd", "Reno", "other-broker@zz.com", "active"],
    4: [COMPLETED_ADDR, "Austin", BROKER, "completed"],
    5: [PARTIAL_ADDR, "Austin", BROKER, "active"],
}

# Same rows after the broker re-sorted the sheet while an action was pending.
# The partial thread's stored rowNumber (4) is now stale: row 4 holds the
# COMPLETED sibling, and the real partial property has moved to row 6.
SORTED_GRID = {
    3: ["900 Elm Blvd", "Reno", "other-broker@zz.com", "active"],
    4: [COMPLETED_ADDR, "Austin", BROKER, "completed"],  # stale target after sort
    6: [PARTIAL_ADDR, "Austin", BROKER, "active"],       # durable location of the partial
}


class SameBrokerMultiPropertySplitSubjectDeck(unittest.TestCase):

    # =====================================================================
    # mustProve #1 -- only intended row updates.
    # A same-broker reply whose visible subject names the PARTIAL property is
    # anchored to the partial row alone; the completed sibling is untouched and
    # the different-broker decoy never enters the picture.
    # =====================================================================
    def test_subject_anchors_to_named_property_only_not_sibling(self):
        thread = {"subject": f"Re: {PARTIAL_ADDR}, Austin", "rowNumber": 5}
        rownum, rowvals = _run_anchor(thread, LAUNCH_GRID, fallback_email=BROKER)
        self.assertEqual(rownum, 5, "reply must land on the partial property row only")
        self.assertEqual(rowvals[0], PARTIAL_ADDR)
        self.assertEqual(rowvals[3], "active",
                         "must not select the completed sibling row")

    # =====================================================================
    # mustProve #1/#2 -- email alone must NOT resolve a row when the broker
    # owns several. With no usable subject and no stored rowNumber, the guard
    # must REFUSE (fail closed) rather than pick one of the two same-broker
    # rows arbitrarily -- "reply matching must not update all rows or wrong row
    # by email alone".
    # =====================================================================
    def test_email_alone_refuses_ambiguous_same_broker_rows(self):
        thread = {"subject": ""}  # no address, no stored rowNumber
        rownum, rowvals = _run_anchor(thread, LAUNCH_GRID, fallback_email=BROKER)
        self.assertIsNone(rownum,
                          "ambiguous broker email owning 2 rows must not resolve a row")
        self.assertIsNone(rowvals)

    # =====================================================================
    # mustProve #2 -- split thread is linked only when safe/visible.
    # The broker drifts the subject onto the OTHER (completed) property. The
    # matcher must re-link to exactly the property the subject now names -- the
    # completed row -- proving the link follows the visible subject, not the
    # stored anchor or the sibling.
    # =====================================================================
    def test_subject_drift_relinks_to_the_visibly_named_property(self):
        thread = {"subject": f"Re: {COMPLETED_ADDR}, Austin", "rowNumber": 5}  # stored=partial
        rownum, rowvals = _run_anchor(thread, LAUNCH_GRID, fallback_email=BROKER)
        self.assertEqual(rownum, 4, "subject drift must re-link to the named (completed) row")
        self.assertEqual(rowvals[0], COMPLETED_ADDR)

    # =====================================================================
    # mustProve #2 -- and it must link ONLY when safe. An unparseable/blank
    # subject over an ambiguous same-broker inbox gives nothing durable to
    # anchor on, so the split thread must stay UNLINKED (no wrong write).
    # =====================================================================
    def test_unparseable_subject_stays_unlinked(self):
        thread = {"subject": "Re: quick question about your listings"}
        # "quick question about your listings" has no comma -> parses to a bogus
        # address that matches no row; email owns 2 rows -> must refuse.
        rownum, _ = _run_anchor(thread, LAUNCH_GRID, fallback_email=BROKER)
        self.assertIsNone(rownum, "unsafe/unparseable split thread must not be linked")

    # =====================================================================
    # THE COMBINATION BUG (regression guard) -- mustProve #1 + #3.
    # same_broker_multiple_properties x row_move_during_pending_action x
    # complete/partial. After a sort the partial thread's stored rowNumber (4)
    # points at the COMPLETED sibling, whose address ("22 Oak Ave") is a
    # whole-token PREFIX of the partial's ("22 Oak Ave North"). The reply is
    # about the PARTIAL property. The matcher must follow the durable anchor to
    # the partial's true row (6) and must NEVER write partial data onto the
    # completed row (4).
    #
    # Reverting the exact-only stored-row fix makes this FAIL (returns row 4).
    # =====================================================================
    def test_row_move_reanchors_partial_reply_off_completed_sibling(self):
        thread = {"subject": f"Re: {PARTIAL_ADDR}, Austin", "rowNumber": 4}  # stale after sort
        rownum, rowvals = _run_anchor(thread, SORTED_GRID, fallback_email=BROKER)
        self.assertEqual(
            rownum, 6,
            "SAFETY: partial reply must re-anchor to the partial's durable row "
            f"(6), not the stale completed sibling (got row {rownum}).",
        )
        self.assertEqual(rowvals[0], PARTIAL_ADDR)
        self.assertNotEqual(
            rownum, 4,
            "SAFETY: the COMPLETED sibling row must not absorb the partial reply.",
        )
        self.assertEqual(rowvals[3], "active",
                         "resolved row must be the still-active partial property")

    # =====================================================================
    # Guard against over-correcting the fix: a LEGIT prefix (subject appended a
    # region token, and NO exact row exists) must still anchor via the
    # exact-preferring scan. Ensures the exact-only stored-row rule did not
    # break normal region-tag prefixing.
    # =====================================================================
    def test_legit_region_tag_prefix_still_anchors(self):
        grid = {
            3: ["900 Elm Blvd", "Reno", "other-broker@zz.com", "active"],
            4: ["742 Evergreen Terrace", "Springfield", BROKER, "active"],
        }
        # Subject carries an extra region token; row addr is the whole-token prefix.
        thread = {"subject": "742 Evergreen Terrace, Springfield, IL", "rowNumber": 4}
        rownum, rowvals = _run_anchor(thread, grid, fallback_email=BROKER)
        self.assertEqual(rownum, 4, "legit region-tag prefix must still anchor")
        self.assertEqual(rowvals[0], "742 Evergreen Terrace")

    # =====================================================================
    # mustProve #3 -- completed row does not receive a follow-up meant for the
    # partial row. Each property is its own thread. The follow-up terminal guard
    # (real) must BLOCK the completed thread while leaving the partial thread
    # eligible -- so a due follow-up fires only on the partial property, never on
    # the completed sibling the same broker owns.
    # =====================================================================
    def test_completed_thread_blocks_followup_while_partial_stays_eligible(self):
        followup_config = {
            "enabled": True,
            "currentFollowUpIndex": 0,
            "followUps": [{"delayDays": 3}, {"delayDays": 7}],
        }

        completed_thread = {
            "subject": f"Re: {COMPLETED_ADDR}, Austin",
            "rowNumber": 4,
            "status": "completed",
        }
        partial_thread = {
            "subject": f"Re: {PARTIAL_ADDR}, Austin",
            "rowNumber": 5,
            "status": "active",
        }

        completed_block = _followup_terminal_block_reason(completed_thread, followup_config, 0)
        partial_block = _followup_terminal_block_reason(partial_thread, followup_config, 0)

        self.assertIsNotNone(
            completed_block,
            "completed property's thread must be terminally blocked from follow-up",
        )
        self.assertIn("completed", completed_block.lower())
        self.assertIsNone(
            partial_block,
            "the partial property's thread must remain eligible for its follow-up",
        )

    # =====================================================================
    # Cross-guard coherence: the two independent guards must AGREE about the
    # completed sibling. The row matcher must not route the partial reply to the
    # completed row (guard A), AND the follow-up guard must not emit on it
    # (guard B). If either broke, the completed row would be touched by the
    # partial property's automation.
    # =====================================================================
    def test_completed_sibling_untouched_across_both_guards(self):
        # Guard A: partial reply after a sort never lands on the completed row.
        thread = {"subject": f"Re: {PARTIAL_ADDR}, Austin", "rowNumber": 4}
        rownum, _ = _run_anchor(thread, SORTED_GRID, fallback_email=BROKER)
        self.assertNotEqual(rownum, 4)

        # Guard B: the completed thread never emits a follow-up.
        block = _followup_terminal_block_reason(
            {"status": "completed"}, {"enabled": True, "followUps": [{}]}, 0
        )
        self.assertIsNotNone(block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
