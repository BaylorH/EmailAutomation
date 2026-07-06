"""Listing Broker Comments de-duplication — grounded in the REAL MOHR sheet.

Before this fix, _append_notes_to_comments blindly did
    combined = f"{existing} • {notes}"
so every broker reply/update re-appended the same spec facts. Jill's live
Austin South sheet shows the damage: cells like
    "NNN • ... • NNN • ... • NNN"
    "100% HVAC • available now • available now • 100% HVAC"
    "... • tour available • NNN • tour available"
while Jill's own clean "Jills Comments" column is de-duplicated. These tests pin
the clean-merge behavior against those exact real strings.
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

from email_automation.ai_processing import _merge_comment_bullets, _normalize_comment_bullet  # noqa: E402


class MergeCommentBulletsTests(unittest.TestCase):
    def test_first_write_returns_notes_unchanged(self):
        self.assertEqual(_merge_comment_bullets("", "NNN • available now"), "NNN • available now")

    def test_empty_notes_returns_existing(self):
        self.assertEqual(_merge_comment_bullets("NNN • 100% HVAC", ""), "NNN • 100% HVAC")

    def test_exact_duplicate_fact_not_reappended(self):
        # The classic real-sheet defect: "NNN" already present, do not re-add.
        out = _merge_comment_bullets("NNN • 100% HVAC", "NNN")
        self.assertEqual(out, "NNN • 100% HVAC")
        self.assertEqual(out.lower().count("nnn"), 1)

    def test_real_row28_double_dup_collapses(self):
        # Real Jill row 28 accumulated: "100% HVAC • available now • available now • 100% HVAC"
        # Simulate the two updates that produced it and assert a clean merge.
        first = "100% HVAC • available now"
        out = _merge_comment_bullets(first, "available now • 100% HVAC")
        self.assertEqual(out, "100% HVAC • available now")

    def test_real_row9_triple_nnn_collapses(self):
        # Real Jill row 9 had NNN three times interleaved with distinct facts.
        existing = "NNN • parking 2.3/1,000 • Suite 190 available 7/1/26"
        notes = "NNN • recently made-ready • 100% HVAC • office-heavy • NNN"
        out = _merge_comment_bullets(existing, notes)
        self.assertEqual(out.lower().count("nnn"), 1)
        # every distinct fact is preserved, in first-seen order
        for fact in ("parking 2.3/1,000", "Suite 190 available 7/1/26",
                     "recently made-ready", "100% HVAC", "office-heavy"):
            self.assertIn(fact, out)

    def test_new_distinct_facts_are_appended(self):
        out = _merge_comment_bullets("NNN • available now", "TI allowance available • 3 dock doors")
        self.assertEqual(out, "NNN • available now • TI allowance available • 3 dock doors")

    def test_case_and_whitespace_insensitive_dedup(self):
        out = _merge_comment_bullets("100% HVAC", "100%   hvac")
        self.assertEqual(out, "100% HVAC")

    def test_internal_cr_normalized_for_dedup(self):
        # A stray CR *inside* a bullet ("available\rnow", where a \r replaced a
        # space in the live cell) must normalize so the identical fact dedups.
        # The outer .strip() only removes a *trailing* CR, so a mid-string CR is
        # what actually exercises _normalize_comment_bullet's \r->space replace.
        self.assertEqual(_normalize_comment_bullet("available\rnow"), "available now")
        out = _merge_comment_bullets("available\rnow", "available now")
        # dedup collapsed the duplicate → a single bullet, no separator added
        self.assertNotIn("•", out)

    def test_trailing_cr_normalized(self):
        # Real cells also carry a stray *trailing* \r (row3 "...total space is 7,920SF.r").
        out = _merge_comment_bullets("available now\r", "available now")
        # first-seen surface form kept, no duplicate
        self.assertEqual(out.count("available now"), 1)

    def test_dated_event_lines_always_kept(self):
        # Terminal event append lines are event-specific and must never be
        # dedup-dropped even if they resemble an earlier line.
        existing = "NNN • [06/09/2026] Property marked unavailable - contact said: 'no longer available'"
        notes = "[06/12/2026] Property marked unavailable - contact said: 'no longer available'"
        out = _merge_comment_bullets(existing, notes)
        self.assertIn("[06/09/2026]", out)
        self.assertIn("[06/12/2026]", out)

    def test_order_preserved_existing_before_new(self):
        out = _merge_comment_bullets("A • B", "C • A • D")
        self.assertEqual(out, "A • B • C • D")


if __name__ == "__main__":
    unittest.main()
