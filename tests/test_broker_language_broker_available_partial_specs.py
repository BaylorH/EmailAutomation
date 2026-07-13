"""Pressure test: broker_available_partial_specs.

A broker says the property IS available and hands over SOME specs, while other
required fields are still TBD / pending / "I'll follow up".

Safety contract (from the event's stopIf rules):
  * Partial data must NOT let the row be marked completed.
  * The follow-up must chase the STILL-MISSING fields (never repeat known ones).

Deterministic guards exercised (no LLM, no network):
  * ai_processing.check_missing_required_fields  -> the completion guard. If any
    required cell is empty it reports it, keeping the row open so the pipeline
    sends a "thanks + please send X" reply (processing.py SCENARIO 3/4).
  * ai_processing._augment_events_with_deterministic_signals -> reads the latest
    broker text and can force property_unavailable / tour_requested. For plain
    partial-availability it must stay silent (no false non-viable); for a clear
    non-fit reason it must fire property_unavailable (near-miss #1); it must not
    resurrect a STALE quoted "no longer available" over newer facts (near-miss #2).
  * ai_processing._latest_inbound_text -> newest inbound wins over older ones.

Only external boundaries would be Firestore/Sheets/Graph; none of the functions
under test touch them, so nothing needs patching. We never send or write.
"""

import os
import unittest

os.environ.setdefault("E2E_TEST_MODE", "true")

from email_automation.ai_processing import (
    check_missing_required_fields,
    _augment_events_with_deterministic_signals,
    _latest_inbound_text,
)

# Canonical sheet header with every required-for-close column present.
HEADER = [
    "Property Address", "City", "Total SF", "Ops Ex /SF",
    "Drive Ins", "Docks", "Ceiling Ht", "Power", "Flyer / Link", "Rent/SF /Yr",
]

# Column index helper (0-based) for building partial rows.
IDX = {name: i for i, name in enumerate(HEADER)}


def _row(**vals):
    row = [""] * len(HEADER)
    row[IDX["Property Address"]] = "123 Trade Center Ct"
    row[IDX["City"]] = "Dallas"
    for k, v in vals.items():
        row[IDX[k]] = v
    return row


def _inbound(text):
    return [{"direction": "inbound", "content": text}]


# ---------------------------------------------------------------------------
# 16 realistic partial-specs broker phrasings (terse, verbose, typo'd, ALLCAPS,
# regional, multi-intent, signature block, drip-feed).
# ---------------------------------------------------------------------------
PARTIAL_PHRASINGS = [
    "The space is available; I can confirm 30,000 SF and 24' clear but need to check rate.",
    "We have availability and a floorplan attached, but opex is still TBD.",
    "Yes, it could work. Let me get you power and door details.",
    "avail. 30k sf, 24ft clear. rate + opex tbc, will follow up.",
    "Great news, the suite is still on the market! Confirmed 22,000 SF, 2 dock doors. "
    "I need to double-check ceiling height and power with the landlord before I send those over.",
    "YES ITS AVAILABLE. 15000 SF, 1 DRIVE IN. STILL WAITING ON POWER SPECS.",
    "Space is open. Can confirm total sf and docks. Getting back to you on ceiling ht.",
    "Hi - property is available. Attaching flyer. Rate to follow once I hear from ownership.",
    "we do have space available, roughly 40,000 sf. dock and drive-in counts im confirming now.",
    "Available. 50k SF. Power is 2000A. Opex and ceiling height pending landlord confirmation.",
    "Yep still open! sending specs in pieces - 12,000 SF confirmed, docks TBD.",
    "The unit remains available. I can share SF and clear height today; opex numbers next week.",
    "Confirmed available. Full specs partially ready: 33,000 SF, 26' clear. Need to verify dock/drive-in.",
    "Property available. Numbers coming - SF is 18k, power TBD, docks TBD.",
    "It could work for your client. Let me pull power and dock details and revert.",
    "Still available as of today. 25,000 SF. Rate and opex to be confirmed.\n\n"
    "Best,\nJane Doe | ABC Realty | 555-1234",
]


class PartialAvailabilityDoesNotMisfireEvents(unittest.TestCase):
    """A plain partial-availability reply must NOT be forced non-viable or into a
    tour by the deterministic augmenter. Firing here = false positive that yanks
    a live, in-progress lead off the board."""

    def test_no_property_unavailable_or_tour_on_partial_specs(self):
        for text in PARTIAL_PHRASINGS:
            with self.subTest(text=text[:48]):
                proposal = {"events": [], "updates": []}
                out = _augment_events_with_deterministic_signals(proposal, _inbound(text))
                types = [(e or {}).get("type") for e in out.get("events", [])]
                self.assertNotIn(
                    "property_unavailable", types,
                    f"FALSE POSITIVE: partial availability marked non-viable: {text!r}",
                )
                self.assertNotIn(
                    "tour_requested", types,
                    f"FALSE POSITIVE: partial availability turned into a tour: {text!r}",
                )


class PartialSpecsKeepsRowOpen(unittest.TestCase):
    """The completion guard must report the still-empty required fields so the row
    cannot close on partial data (stopIf: 'partial data marks the row completed')."""

    # (label, filled-cells) — each leaves >=1 required cell genuinely empty.
    PARTIAL_ROWS = [
        ("sf+clear only", dict(**{"Total SF": "30000", "Ceiling Ht": "24"})),
        ("sf+docks", dict(**{"Total SF": "22000", "Docks": "2"})),
        ("sf+power", dict(**{"Total SF": "50000", "Power": "2000A"})),
        ("sf only", dict(**{"Total SF": "18000"})),
        ("sf+driveins", dict(**{"Total SF": "40000", "Drive Ins": "1"})),
        ("sf+opex", dict(**{"Total SF": "25000", "Ops Ex /SF": "5.50"})),
        ("sf+flyer", dict(**{"Total SF": "12000", "Flyer / Link": "http://x/flyer.pdf"})),
        ("everything but power", dict(**{
            "Total SF": "30000", "Ops Ex /SF": "5", "Drive Ins": "2",
            "Docks": "4", "Ceiling Ht": "24", "Flyer / Link": "http://x"})),
        ("everything but docks", dict(**{
            "Total SF": "30000", "Ops Ex /SF": "5", "Drive Ins": "2",
            "Ceiling Ht": "24", "Power": "1200A", "Flyer / Link": "http://x"})),
        ("everything but ceiling", dict(**{
            "Total SF": "30000", "Ops Ex /SF": "5", "Drive Ins": "2",
            "Docks": "4", "Power": "1200A", "Flyer / Link": "http://x"})),
        ("everything but opex", dict(**{
            "Total SF": "30000", "Drive Ins": "2", "Docks": "4",
            "Ceiling Ht": "24", "Power": "1200A", "Flyer / Link": "http://x"})),
        ("everything but flyer", dict(**{
            "Total SF": "30000", "Ops Ex /SF": "5", "Drive Ins": "2",
            "Docks": "4", "Ceiling Ht": "24", "Power": "1200A"})),
        ("sf+clear+docks", dict(**{"Total SF": "33000", "Ceiling Ht": "26", "Docks": "3"})),
        ("only rent set", dict(**{"Rent/SF /Yr": "12.00"})),
        ("nothing filled", dict()),
    ]

    def test_partial_rows_report_missing_required_fields(self):
        for label, filled in self.PARTIAL_ROWS:
            with self.subTest(label=label):
                missing = check_missing_required_fields(_row(**filled), HEADER)
                # Rent/SF is intentionally NOT in the required-for-close set.
                self.assertTrue(
                    missing,
                    f"COMPLETION HOLE: '{label}' row reported complete on partial data",
                )


class PlaceholderValueDefeatsCompletionGuard(unittest.TestCase):
    """A required cell holding a placeholder ('TBD', 'pending', 'TBC', 'N/A', '?')
    is NOT a real spec. The completion guard must still treat it as missing,
    otherwise a broker's 'opex is still TBD' closes the row on non-data.

    Current behaviour: check_missing_required_fields only tests truthiness of the
    cell, so any non-empty placeholder satisfies it -> these assertions are RED
    and pin the bug.
    """

    PLACEHOLDERS = ["TBD", "tbd", "TBC", "pending", "N/A", "?", "to follow", "ask landlord"]

    def test_placeholder_opex_is_still_missing(self):
        for ph in self.PLACEHOLDERS:
            with self.subTest(placeholder=ph):
                row = _row(**{
                    "Total SF": "30000", "Ops Ex /SF": ph, "Drive Ins": "2",
                    "Docks": "4", "Ceiling Ht": "24", "Power": "1200A",
                    "Flyer / Link": "http://x/flyer.pdf",
                })
                missing = check_missing_required_fields(row, HEADER)
                self.assertIn(
                    "Ops Ex /SF", missing,
                    f"COMPLETION HOLE: placeholder {ph!r} accepted as a real Ops Ex value; "
                    f"row can close on partial data",
                )


class NearMissNonFitBecomesNonViable(unittest.TestCase):
    """NEAR-MISS #1: partial details + a clear non-fit reason must become
    non-viable (property_unavailable), NOT fall through to 'ask for more specs'."""

    NONFIT_PHRASINGS = [
        "I can confirm 30,000 SF but this space won't be a good fit for your client - it's mostly office.",
        "30,000 SF confirmed, but it's more office-heavy as opposed to a true warehouse and has no drive-in space.",
        "SF is 40k, but honestly this isn't the right fit for your client's warehouse needs.",
        # Very common casual spelling WITHOUT the apostrophe:
        "I can confirm 30,000 SF but this space wont be a good fit for your client - it's mostly office.",
    ]

    def test_partial_plus_nonfit_marks_unavailable(self):
        for text in self.NONFIT_PHRASINGS:
            with self.subTest(text=text[:48]):
                proposal = {"events": [], "updates": []}
                out = _augment_events_with_deterministic_signals(proposal, _inbound(text))
                types = [(e or {}).get("type") for e in out.get("events", [])]
                self.assertIn(
                    "property_unavailable", types,
                    f"FALSE NEGATIVE: non-fit rejection not marked non-viable; "
                    f"pipeline will chase specs on a rejected property: {text!r}",
                )


class NearMissQuotedHistoryNoOverwrite(unittest.TestCase):
    """NEAR-MISS #2: the newest message says the space is available again and
    sends partial specs; an OLDER quoted block still reads 'no longer available'.
    The stale quoted line must NOT overwrite newer facts by forcing non-viable.
    """

    def test_stale_quoted_unavailable_does_not_win(self):
        msg = (
            "Good news - the space is back on the market and available again. "
            "I can confirm 30,000 SF, opex TBD.\n\n"
            "On Mon, Broker wrote:\n"
            "> Unfortunately this space is no longer available, it's fully leased."
        )
        proposal = {"events": [], "updates": []}
        out = _augment_events_with_deterministic_signals(proposal, _inbound(msg))
        types = [(e or {}).get("type") for e in out.get("events", [])]
        self.assertNotIn(
            "property_unavailable", types,
            "FALSE POSITIVE: stale quoted 'no longer available' overrode the newer "
            "'available again' text and marked the property non-viable",
        )


class LatestInboundPicksNewest(unittest.TestCase):
    """Control: newest inbound message is the source of truth over older ones."""

    def test_newest_inbound_wins(self):
        conv = [
            {"direction": "inbound", "content": "Property is no longer available."},
            {"direction": "outbound", "content": "Thanks for letting me know."},
            {"direction": "inbound", "content": "Update - it's available again, 30,000 SF, opex TBD."},
        ]
        self.assertIn("available again", _latest_inbound_text(conv))


if __name__ == "__main__":
    unittest.main(verbosity=2)
