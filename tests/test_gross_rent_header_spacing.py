"""The Gross Rent formula must survive the header spellings real sheets use.

LIVE break, 2026-08-06 production campaign, recorded as one of eight defects:
"missing Gross Rent formula on the replacement row after facts applied".

Root cause is not the formula. It is `_find_header_position`, which matches a
header by bare `.strip().lower()`, so 'Ops Ex / SF' -- the spelling on the real
sheet -- fails to equal either alias ('ops ex /sf', 'ops ex/sf'). The builder
then returns None and `_apply_gross_rent_formula_for_row` returns False WITHOUT
raising, so a replacement row silently ships with no Gross Rent formula. Gross
Rent is the column the customer actually screens on, so the row looks finished
and is unusable.

This exact class already bit this project once, on the same header: see
`ai_processing._normalize_required_col_key`, whose docstring names
'Ops Ex / SF' == 'Ops Ex /SF' and which was written to fix
check_missing_required_fields after it re-requested ALREADY-FILLED columns
forever. That normalizer was never applied to `_find_header_position`, so the
same spacing bug stayed live in the formula path.

These tests drive the REAL header spellings, not invented ones.
"""

import unittest

from email_automation.sheet_operations import (
    _build_gross_rent_formula_for_row,
    _find_header_position,
)

# The spelling on the real customer sheet, recorded verbatim in
# ai_processing.py's _normalize_required_col_key docstring and its LIVE break note.
REAL_HEADER = ["Property Address", "City", "Total SF", "Rent/SF /Yr", "Ops Ex / SF", "Gross Rent"]


class GrossRentSurvivesRealHeaderSpacing(unittest.TestCase):
    def test_the_real_sheet_spelling_still_locates_the_ops_ex_column(self):
        self.assertIsNotNone(
            _find_header_position(REAL_HEADER, ["ops ex /sf", "ops ex/sf"]),
            "'Ops Ex / SF' is the spelling on the live sheet; failing to match it "
            "is what silently dropped the Gross Rent formula",
        )

    def test_the_formula_is_built_for_the_real_sheet(self):
        built = _build_gross_rent_formula_for_row(REAL_HEADER, 8)
        self.assertIsNotNone(
            built, "a replacement row on the real sheet must still get its Gross Rent formula"
        )
        column, formula = built
        self.assertEqual(column, "F")
        self.assertIn("E8", formula, "the formula must reference the Ops Ex column it found")

    def test_spacing_and_punctuation_variants_all_resolve_to_the_same_column(self):
        # Every spelling a human or an export might produce for one column.
        for spelling in ("Ops Ex / SF", "Ops Ex /SF", "Ops Ex/SF", "OPS EX / SF", "ops ex / sf"):
            with self.subTest(spelling=spelling):
                header = list(REAL_HEADER)
                header[4] = spelling
                self.assertIsNotNone(
                    _build_gross_rent_formula_for_row(header, 8),
                    f"{spelling!r} names the same column as every other spelling here",
                )

    def test_a_genuinely_absent_column_still_refuses(self):
        # Vacuity guard: normalization must not make everything match.
        header = ["Property Address", "City", "Total SF", "Rent/SF /Yr", "Notes", "Gross Rent"]
        self.assertIsNone(
            _build_gross_rent_formula_for_row(header, 8),
            "a sheet with no Ops Ex column has no Gross Rent formula to build",
        )


if __name__ == "__main__":
    unittest.main()
