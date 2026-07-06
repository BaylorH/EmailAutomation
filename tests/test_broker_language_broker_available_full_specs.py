"""
Pressure test for broker-event class: broker_available_full_specs

The broker replies "yes, available" and dumps full specs, including an asking
rate. The deterministic guard that decides the *economics* half of this event is
`email_automation.ai_processing._extract_rent_sf_yr_from_text` (and its wrapper
`_augment_proposal_with_deterministic_extractions`, which writes the parsed value
straight into the "Rent/SF /Yr" sheet column with confidence 0.92).

stopIf conditions for this event:
  - "system asks for fields already provided"   -> false negative if the guard
    fails to capture an asking rate the broker clearly stated (rent stays blank,
    downstream re-asks for it).
  - "unsourced economics are invented"          -> false positive if the guard
    parses a NON-rent dollar/SF figure (TI allowance, taxes, parking, buildout)
    and writes it into the asking-rent column.

These are PURE functions (no Firestore / Sheets / Graph), so nothing external is
touched. Assertions pin the CORRECT behavior; assertions that currently fail mark
real defects and are left RED intentionally.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.ai_processing import (
    _extract_rent_sf_yr_from_text,
    _augment_proposal_with_deterministic_extractions,
    _looks_like_requirements_mismatch_nonviable,
)


# ---------------------------------------------------------------------------
# Real-threat phrasings: broker states an asking rate. Guard SHOULD extract it.
# value = expected annualized $/SF/yr string, or None where we deem it genuinely
# unextractable. Cases marked FN are asking rates the guard MISSES (false neg).
# ---------------------------------------------------------------------------
REAL_ASKING_RATES = [
    # (phrasing, expected_extraction)
    ("Asking $9.75/SF/yr gross.", "9.75"),
    ("Base rent $12.50 per sf.", "12.50"),
    ("Rate is $0.85/sf/mo NNN", "10.20"),                    # monthly -> annual
    ("asking rate $18 per square foot", "18.00"),
    ("Rent: $22.50/SF/year", "22.50"),
    ("ASKING $15/SF NNN", "15.00"),                          # all caps
    ("rael rate $11/sf", "11.00"),                           # typo, still $/sf
    ("The asking rental rate for this space is $10.25 per "
     "square foot per year on a NNN basis.", "10.25"),       # verbose
    ("$0.82/SF/month NNN", "9.84"),                          # monthly per-sf
    # --- Seed fragments. These are the literal full-spec seeds. Broker clearly
    #     gave an asking rate, so the guard SHOULD capture it. Currently RED. ---
    ("We can do 50k SF, $9.75 gross, ESFR, 2,000A, "
     "available September 1.", "9.75"),                      # SEED 3 -> FN (None)
    ("Yes, available. 42,000 SF, $0.82 NNN, $0.21 opex, "
     "28' clear, 4 docks, 1 drive-in.", "9.84"),            # SEED 1: $0.82/mo NNN
]


# ---------------------------------------------------------------------------
# Near-miss / control phrasings: a $/SF figure that is NOT the asking rent.
# Guard MUST return None (else it invents unsourced economics).
# ---------------------------------------------------------------------------
NON_RENT_DOLLAR_PER_SF = [
    "TI allowance of $15/SF",
    "$25/SF tenant improvement allowance",
    "Parking at $3/SF",
    "Real estate taxes $4.10/SF",
    "Buildout runs $50 per sf",
    "Taxes are $2.50/SF",
]

# Controls the guard already handles correctly (must stay None / pass).
CORRECTLY_AVOIDED = [
    "CAM charges $3.50/SF",
    "opex is $0.21/SF",
    "Operating expenses $4/SF",
    "$0.21 opex",                        # seed near-miss, no /sf token
]


class TestFullSpecsRentExtraction(unittest.TestCase):
    def test_real_asking_rates_are_extracted_no_false_negative(self):
        """Every clearly-stated asking rate must be captured (no re-asking)."""
        failures = []
        for phrasing, expected in REAL_ASKING_RATES:
            got = _extract_rent_sf_yr_from_text(phrasing)
            if got != expected:
                failures.append(f"{phrasing!r}: expected {expected!r}, got {got!r}")
        self.assertEqual(
            failures, [],
            "Guard missed asking rates the broker explicitly provided "
            "(-> system will re-ask for fields already given):\n" + "\n".join(failures),
        )

    def test_non_rent_dollar_per_sf_is_not_parsed_as_rent(self):
        """TI / taxes / parking / buildout $/SF must NOT become the asking rent."""
        invented = []
        for phrasing in NON_RENT_DOLLAR_PER_SF:
            got = _extract_rent_sf_yr_from_text(phrasing)
            if got is not None:
                invented.append(f"{phrasing!r} -> invented asking rent {got!r}")
        self.assertEqual(
            invented, [],
            "Guard invented unsourced economics from non-rent figures:\n"
            + "\n".join(invented),
        )

    def test_correctly_avoided_controls_stay_none(self):
        """Sanity: opex/CAM-labeled and no-/sf figures already return None."""
        for phrasing in CORRECTLY_AVOIDED:
            self.assertIsNone(
                _extract_rent_sf_yr_from_text(phrasing),
                f"Control regressed and now parses a rent: {phrasing!r}",
            )


class TestFullSpecsProposalAugmentation(unittest.TestCase):
    """End-to-end: the wrapper writes the parsed value into the rent column."""

    HEADER = ["Property Address", "City", "Rent/SF /Yr"]
    CONFIG = {"mappings": {"rent_sf_yr": "Rent/SF /Yr"}}

    def _augment(self, broker_text):
        rowvals = ["123 Main St", "Austin", ""]  # rent column empty
        conversation = [{"direction": "inbound", "content": broker_text}]
        proposal = {"updates": []}
        return _augment_proposal_with_deterministic_extractions(
            proposal, rowvals, self.HEADER, self.CONFIG, conversation
        )

    def test_ti_allowance_not_written_as_asking_rent(self):
        """A TI allowance must NOT be written into the asking-rent column."""
        out = self._augment(
            "Yes, available. 50,000 SF. TI allowance of $25/SF, 28' clear, 4 docks."
        )
        rent_updates = [u for u in out["updates"] if u.get("column") == "Rent/SF /Yr"]
        self.assertEqual(
            rent_updates, [],
            "Invented economics: a TI allowance was written into 'Rent/SF /Yr' "
            f"as the asking rate -> {rent_updates}",
        )

    def test_taxes_not_written_as_asking_rent(self):
        out = self._augment(
            "Available now, 30,000 SF. Real estate taxes $4.10/SF, ESFR sprinklers."
        )
        rent_updates = [u for u in out["updates"] if u.get("column") == "Rent/SF /Yr"]
        self.assertEqual(
            rent_updates, [],
            f"Invented economics: taxes written as asking rent -> {rent_updates}",
        )

    def test_genuine_asking_rate_is_written(self):
        """Positive control: a real asking rate SHOULD populate the column."""
        out = self._augment("Available. 20,000 SF, asking $12.50/SF/yr NNN.")
        rent_updates = [u for u in out["updates"] if u.get("column") == "Rent/SF /Yr"]
        self.assertEqual(
            [u.get("value") for u in rent_updates], ["12.50"],
            "Guard failed to capture a genuine asking rate into the column "
            f"-> {out['updates']}",
        )


class TestRequirementsMismatchNearMiss(unittest.TestCase):
    """Near-miss: 'available for office use only while warehouse req unmet'.

    This should NOT be treated as a clean available-full-specs event; the
    requirements-mismatch detector is the deterministic surface that would flag
    it. Recorded here for coverage of the near-miss control.
    """

    def test_office_only_warehouse_unmet_is_flagged_mismatch(self):
        text = ("Available for office use only while warehouse requirement "
                "remains unmet.")
        self.assertTrue(
            _looks_like_requirements_mismatch_nonviable(text),
            "Office-only-while-warehouse-unmet near-miss was NOT flagged as a "
            "requirements mismatch; deterministic guard leaves it to the LLM.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
