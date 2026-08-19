"""The drift tripwire must actually bite.

A classifier that quietly returns "other" for everything reports 0% apparatus
share -- i.e. "all clear" -- which is the exact false-green this project has
been refuted by five times. A tripwire that cannot fire is worse than none,
because it is read as reassurance.
"""

import importlib.util
import pathlib
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "drift_check", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "drift_check.py"
)
drift_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drift_check)


class TheClassifierDistinguishesProductFromApparatus(unittest.TestCase):
    def test_real_product_commit_subjects_classify_as_product(self):
        # Taken verbatim from this branch's history.
        for subject in (
            "The real sheet spells it 'Ops Ex / SF', so Gross Rent was never written",
            "fix: put the recipient filter back on the critical path in surface D-6",
            "A tour on the replacement property was suppressed by the original's key",
        ):
            with self.subTest(subject=subject):
                self.assertIn(drift_check.classify(subject), {"product", "both"})

    def test_real_apparatus_commit_subjects_classify_as_apparatus(self):
        for subject in (
            "Mutation found three twin controls that were only decorative",
            "91 canonical-JSON vectors, pinned as constants, run on both interpreters",
            "docs: record finish-line live certification",
        ):
            with self.subTest(subject=subject):
                self.assertIn(drift_check.classify(subject), {"apparatus", "both"})

    def test_a_commit_naming_both_is_never_counted_as_product(self):
        # Conservative on purpose: ambiguity must not flatter the ratio.
        self.assertEqual(
            drift_check.classify("certify the broker reply extraction scenario"), "both"
        )

    def test_the_classifier_is_not_vacuous(self):
        # If every subject fell into one bucket the ratio would be meaningless.
        buckets = {
            drift_check.classify(s)
            for s in (
                "fix broker reply extraction",
                "add certification scenario registry",
                "certify the broker reply",
                "bump version",
            )
        }
        self.assertGreaterEqual(len(buckets), 3, f"classifier collapsed to {buckets}")

    def test_the_warn_line_is_a_real_threshold(self):
        self.assertTrue(0 < drift_check.WARN_SHARE < 1)


if __name__ == "__main__":
    unittest.main()
