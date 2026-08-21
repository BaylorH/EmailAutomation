"""The check that replaces asking permission before a launch.

The standing test identities are pre-authorized, so the useful question is never
"may I send" but "does this specific campaign reach anyone it must not, and is
anything else already in flight". That has to be answered mechanically, because
the failure it guards against is a stray address nobody noticed -- which by
definition is not something a careful read catches.

Scanning only the Email column is the mistake this is built around: an address in
a comments cell is still an address the product can act on.
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

_spec = importlib.util.spec_from_file_location(
    "preflight_campaign", os.path.join(REPO_ROOT, "scripts", "preflight_campaign.py")
)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)


class AddressDetectionTests(unittest.TestCase):
    def test_it_finds_an_address_buried_in_prose(self):
        found = preflight.addresses_in_text(
            "per Dana, loop in dgee@gardenstaterealty.net on this one"
        )
        self.assertIn("dgee@gardenstaterealty.net", found)

    def test_it_finds_several_and_lowercases_them(self):
        found = preflight.addresses_in_text("A@Example.COM and b+tag@sub.example.co.uk")
        self.assertIn("a@example.com", found)
        self.assertIn("b+tag@sub.example.co.uk", found)

    def test_plus_aliases_survive_detection(self):
        """A plus-alias must be *detected* so it can then be judged, not skipped."""
        found = preflight.addresses_in_text("bp21harrison+row7@gmail.com")
        self.assertIn("bp21harrison+row7@gmail.com", found)

    def test_prose_without_an_address_finds_nothing(self):
        self.assertEqual(preflight.addresses_in_text("no addresses at all here"), {})
        self.assertEqual(preflight.addresses_in_text(""), {})
        self.assertEqual(preflight.addresses_in_text(None), {})


class AllowListIsSharedTests(unittest.TestCase):
    """One allow-list in the repo, not two. Two copies is one copy quietly wrong."""

    def test_the_predicate_is_the_audits_own(self):
        from importlib.util import module_from_spec, spec_from_file_location

        spec = spec_from_file_location(
            "audit_send_exposure",
            os.path.join(REPO_ROOT, "scripts", "audit_send_exposure.py"),
        )
        audit = module_from_spec(spec)
        spec.loader.exec_module(audit)
        for probe in [
            "bp21harrison@gmail.com",
            "bp21harrison+row3@gmail.com",
            "dgee@gardenstaterealty.net",
            "bp21harrison@gmail.com.attacker.net",
        ]:
            with self.subTest(probe=probe):
                self.assertEqual(preflight.allowed(probe), audit.allowed(probe))

    def test_a_real_broker_address_is_refused(self):
        self.assertFalse(preflight.allowed("dgee@gardenstaterealty.net"))

    def test_the_owned_accounts_are_permitted(self):
        self.assertTrue(preflight.allowed("bp21harrison+ev1@gmail.com"))
        self.assertTrue(preflight.allowed("baylor.freelance@outlook.com"))


class WorkbookScanTests(unittest.TestCase):
    """Every cell of every sheet, not the column we meant to fill."""

    def _book(self, path, comment):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Properties"
        ws.append(["Title"])
        ws.append(["Property Address", "Email", "Listing Brokers Comments"])
        ws.append(["1 Test St", "bp21harrison+row3@gmail.com", comment])
        wb.save(path)

    def test_an_address_outside_the_email_column_is_found(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.xlsx")
            self._book(path, "cc dgee@gardenstaterealty.net please")
            found = preflight.addresses_in_workbook(path)
            self.assertIn("dgee@gardenstaterealty.net", found)
            self.assertTrue(
                any("C3" in loc for loc in found["dgee@gardenstaterealty.net"]),
                "the exact cell must be named -- a bare count cannot be acted on",
            )

    def test_a_clean_workbook_yields_only_owned_addresses(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.xlsx")
            self._book(path, "nothing to see here")
            found = preflight.addresses_in_workbook(path)
            self.assertEqual(set(found), {"bp21harrison+row3@gmail.com"})
            self.assertTrue(all(preflight.allowed(a) for a in found))


if __name__ == "__main__":
    unittest.main()
