import unittest

from email_automation.sheet_operations import _build_gross_rent_formula_for_row


class GrossRentFormulaTests(unittest.TestCase):
    def test_formula_handles_single_rent_values_and_rent_ranges(self):
        header = ["Property Address", "Total SF", "Rent/SF /Yr", "Ops Ex /SF", "Gross Rent"]

        gross_col, formula = _build_gross_rent_formula_for_row(header, 4)

        self.assertEqual(gross_col, "E")
        self.assertIn("IFERROR(VALUE(C4)", formula)
        self.assertIn('SPLIT(C4,"-")', formula)
        self.assertIn("*B4/12", formula)


if __name__ == "__main__":
    unittest.main()
