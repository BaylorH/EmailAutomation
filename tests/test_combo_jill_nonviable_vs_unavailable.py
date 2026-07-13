"""Combination stress deck: jill_nonviable_vs_unavailable.

Deck (docs/release-safety/feature-gradebook.json → combinationStressDecks):
    playbooks:
      - tour_unavailable_but_property_viable
      - partial_specs_plus_pdf_plus_followup
      - subject_drift_split_thread
    variantsToCross:
      - "office-heavy non-fit"                (requirements_mismatch → non-viable)
      - "space unavailable"                   (terminal → unavailable)
      - "tour unavailable but property viable"(tour_unavailable → stays viable)
      - "quoted old positive reply under new negative reply"
    mustProve:
      - classifier keeps these states distinct
      - sheet/results display the right reason
      - follow-up only happens for missing information

This is a REAL-handler integration test. It drives the production classifier
end-to-end through ai_processing.propose_sheet_updates (the same function the
inbound webhook calls), faking ONLY the OpenAI boundary (client.responses.create)
and — for the sheet-write leg — the Google Sheets service. No live sends, no live
sheet writes, no Graph/Firestore. dry_run=True skips Firestore logging, and the
conversation is passed directly so build_conversation_payload never touches Graph.

The interaction the deck stresses: four broker "states" (non-viable-by-fit,
unavailable-terminal, tour-unavailable-but-viable, and a stale positive quoted
under a fresh negative) that all superficially look like "the property is gone"
but must be classified DISTINCTLY. The hard case — a single broker message that
BOTH declines tours AND rules the property out on a physical non-fit — must
resolve to the non-viable/terminal state, not the still-alive tour state; if the
tour idiom is allowed to mask the non-fit, the automation keeps chatting with a
dead lead. This test drives the real classifier so it FAILS if any of those
states collapses into another.
"""

import json
import os

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

import unittest
from unittest.mock import MagicMock, patch

import email_automation.ai_processing as ai
from email_automation.ai_processing import (
    apply_proposal_to_sheet,
    check_missing_required_fields,
    get_row_anchor,
)
from email_automation.column_config import detect_column_mapping


# ---------------------------------------------------------------------------
# OpenAI boundary fake — returns whatever proposal JSON the scenario supplies as
# the model's raw output. Everything downstream (deterministic event guards,
# extraction backstops, sheet apply) is the REAL production code.
# ---------------------------------------------------------------------------
def _fake_openai_client(proposal_json: str) -> MagicMock:
    resp = MagicMock()
    resp.output_text = proposal_json
    resp.usage = None
    resp.id = "resp-test"
    client = MagicMock()
    client.responses.create.return_value = resp
    return client


# ---------------------------------------------------------------------------
# Sheets service fake (mirrors tests/test_rubric_cross_feature_lanes.py). Captures
# every batchUpdate so we can assert exactly which cells/rows would be written.
# ---------------------------------------------------------------------------
class _FakeRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeValues:
    def __init__(self):
        self.batch_update_calls = []

    def get(self, spreadsheetId=None, range=None, **kwargs):
        if range and range.startswith("AI_META!"):
            return _FakeRequest({"values": [[
                "rowNumber", "columnName", "last_ai_value",
                "last_ai_write_iso", "human_override", "rowAnchor",
            ]]})
        return _FakeRequest({"values": []})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        self.batch_update_calls.append(body)
        return _FakeRequest({})


class _FakeSpreadsheets:
    def __init__(self, values):
        self._values = values

    def values(self):
        return self._values

    def get(self, spreadsheetId=None, **kwargs):
        return _FakeRequest({"sheets": [
            {"properties": {"title": "Sheet1", "sheetId": 0}},
            {"properties": {"title": "AI_META", "sheetId": 1}},
        ]})

    def batchUpdate(self, spreadsheetId=None, body=None, **kwargs):
        return _FakeRequest({})


class _FakeSheets:
    def __init__(self):
        self.values_api = _FakeValues()
        self.spreadsheets_api = _FakeSpreadsheets(self.values_api)

    def spreadsheets(self):
        return self.spreadsheets_api


HEADER = [
    "Property Address", "City", "Contact Name", "Email",
    "Rent/SF /Yr", "Total SF", "Ops Ex /SF", "Drive Ins",
    "Docks", "Ceiling Ht", "Power", "Flyer / Link", "Notes",
]

COLUMN_CONFIG = detect_column_mapping(HEADER, use_ai=False)
COLUMN_CONFIG["customFields"] = {}


def _row(address, city, contact, email, **cells):
    vals = [address, city, contact, email] + [""] * (len(HEADER) - 4)
    idx = {name: i for i, name in enumerate(HEADER)}
    for col, val in cells.items():
        vals[idx[col]] = val
    return vals


class JillNonviableVsUnavailableDeck(unittest.TestCase):
    """crossFeature: property_status_taxonomy (event_classifier / property_extraction
    / sheet_update / results reason / tour.reply_handling)."""

    # -- helper: drive the REAL classifier through propose_sheet_updates ---------
    def _classify(self, conversation, rowvals, target_row=7, llm_proposal=None):
        """Run production propose_sheet_updates with only OpenAI faked.

        llm_proposal simulates the model's raw JSON. Passing an empty
        events list simulates the model MISSING a signal — which is exactly what
        the deterministic backstop layer exists to catch, so these tests assert on
        the guarded output, not on a cooperative model.
        """
        if llm_proposal is None:
            llm_proposal = {"updates": [], "events": [], "response_email": None}
        latest_inbound = next(
            (m for m in reversed(conversation) if m.get("direction") == "inbound"), {}
        )
        with patch.object(ai, "client", _fake_openai_client(json.dumps(llm_proposal))):
            return ai.propose_sheet_updates(
                uid="uid-1",
                client_id="client-1",
                email=latest_inbound.get("from", "broker@example.com"),
                sheet_id="sheet-1",
                header=HEADER,
                rownum=target_row,
                rowvals=rowvals,
                thread_id="thread-1",
                conversation=conversation,
                column_config=COLUMN_CONFIG,
                extraction_fields=COLUMN_CONFIG["extractionFields"],
                dry_run=True,
            )

    @staticmethod
    def _event_types(proposal):
        return [(e or {}).get("type") for e in (proposal.get("events") or [])]

    @staticmethod
    def _pu_reason(proposal):
        for e in proposal.get("events") or []:
            if (e or {}).get("type") == "property_unavailable":
                return (e or {}).get("reason")
        return None

    @staticmethod
    def _tour_reason(proposal):
        for e in proposal.get("events") or []:
            if (e or {}).get("type") == "tour_requested":
                return (e or {}).get("reason")
        return None

    # -- STATE 1: tour unavailable but the PROPERTY is viable --------------------
    def test_tour_unavailable_keeps_property_viable(self):
        """Playbook tour_unavailable_but_property_viable: a tour-only decline must
        NOT terminalize the row. Distinct state → tour_requested(tour_unavailable),
        never property_unavailable."""
        rowvals = _row("120 Logistics Way", "Reno", "Sam Poe", "sam@brk.com")
        conv = [
            {"direction": "outbound",
             "content": "Following up on 120 Logistics Way, Reno — could we schedule a tour?"},
            {"direction": "inbound", "from": "sam@brk.com", "fromName": "Sam Poe",
             "content": "We can't do tours on that space right now, but it's still on the market and shows well."},
        ]
        out = self._classify(conv, rowvals)
        self.assertNotIn("property_unavailable", self._event_types(out),
                         "tour-only decline must NOT mark the property unavailable")
        self.assertIn("tour_requested", self._event_types(out))
        self.assertEqual("tour_unavailable", self._tour_reason(out))

    # -- STATE 2: the space itself is UNAVAILABLE (terminal) ---------------------
    def test_space_unavailable_is_terminal_with_terminal_reason(self):
        """Variant 'space unavailable': an explicit terminal on the TARGET property
        yields property_unavailable with a terminal (non-'requirements_mismatch')
        reason, and kills any live response_email."""
        rowvals = _row("4820 Jonestown Rd", "Harrisburg", "Dana Vale", "dana@brk.com")
        conv = [
            {"direction": "outbound",
             "content": "Checking in on 4820 Jonestown Rd, Harrisburg."},
            {"direction": "inbound", "from": "dana@brk.com", "fromName": "Dana Vale",
             "content": "That space at 4820 Jonestown Rd is no longer available — it was just leased."},
        ]
        out = self._classify(conv, rowvals, llm_proposal={
            "updates": [], "events": [], "response_email": "Thanks — any similar space?"})
        self.assertIn("property_unavailable", self._event_types(out))
        reason = self._pu_reason(out)
        self.assertIsNotNone(reason)
        self.assertNotEqual("requirements_mismatch", reason,
                            "a leased/off-market terminal is 'unavailable', not a fit-mismatch")
        self.assertIsNone(out.get("response_email"),
                          "a terminal row must not keep chatting with the broker")

    # -- STATE 3: physical non-fit (office-heavy) → NON-VIABLE -------------------
    def test_office_heavy_nonfit_is_requirements_mismatch(self):
        """Variant 'office-heavy non-fit': a physical requirements failure is
        non-viable with the DISTINCT reason 'requirements_mismatch'."""
        rowvals = _row("77 Rivergate Pkwy", "Nashville", "Lee Park", "lee@brk.com")
        conv = [
            {"direction": "outbound",
             "content": "Is 77 Rivergate Pkwy a fit for a warehouse user?"},
            {"direction": "inbound", "from": "lee@brk.com", "fromName": "Lee Park",
             "content": "Honestly this one is too office-heavy for your client's warehouse needs — not a true warehouse."},
        ]
        out = self._classify(conv, rowvals)
        self.assertIn("property_unavailable", self._event_types(out))
        self.assertEqual("requirements_mismatch", self._pu_reason(out))

    # -- STATE 4: the INTERACTION bug — tour-decline + non-fit in ONE message ----
    def test_tour_decline_does_not_mask_a_genuine_nonfit(self):
        """THE deck's core cross-feature interaction. A single broker reply that
        BOTH declines tours AND rules the property out physically must resolve to
        the non-viable state (requirements_mismatch), not the still-alive tour
        state. If the tour idiom is allowed to short-circuit the non-fit backstop,
        the row stays viable with a live tour response_email and the automation
        keeps working a dead lead."""
        rowvals = _row("910 Cargo Ct", "Memphis", "Ray Nunn", "ray@brk.com")
        conv = [
            {"direction": "outbound",
             "content": "Following up on 910 Cargo Ct, Memphis — tour + confirm warehouse specs?"},
            {"direction": "inbound", "from": "ray@brk.com", "fromName": "Ray Nunn",
             "content": "We can't do tours right now, and honestly the space is too "
                        "office-heavy for your client's warehouse needs."},
        ]
        # The model MISSED the non-fit and only saw a tour decline (worst case the
        # deterministic layer must repair).
        out = self._classify(conv, rowvals, llm_proposal={
            "updates": [], "events": [], "response_email": "Thanks! When could we tour?"})

        self.assertIn("property_unavailable", self._event_types(out),
                      "a genuine physical non-fit must terminalize even when the "
                      "same message also declines tours")
        self.assertEqual("requirements_mismatch", self._pu_reason(out))
        self.assertNotIn("tour_requested", self._event_types(out),
                         "a non-viable row must not also carry a live tour_requested")
        self.assertIsNone(out.get("response_email"),
                          "a non-viable row must not keep a live tour response_email")

    # -- STATE 5: quoted OLD positive under a NEW negative -----------------------
    def test_quoted_positive_history_does_not_mask_new_terminal(self):
        """Variant 'quoted old positive reply under new negative reply': a fresh
        'no longer available' on top must fire even though the broker quoted an
        earlier 'still available / shows well' below. Quoted history must not
        resurrect viability."""
        rowvals = _row("310 Commerce Dr", "Aurora", "Pat Vale", "pat@brk.com")
        latest = (
            "Update: 310 Commerce Dr is no longer available — it just went under contract.\n"
            "\n"
            "On Mon, Jun 30, Pat Vale wrote:\n"
            "> 310 Commerce Dr is still available and shows really well, very much still on the market."
        )
        conv = [
            {"direction": "outbound", "content": "Any update on 310 Commerce Dr, Aurora?"},
            {"direction": "inbound", "from": "pat@brk.com", "fromName": "Pat Vale",
             "content": latest},
        ]
        out = self._classify(conv, rowvals)
        self.assertIn("property_unavailable", self._event_types(out),
                      "a fresh terminal must fire despite a quoted stale 'still available'")
        # And distinctly NOT a fit-mismatch — it is a real unavailability terminal.
        self.assertNotEqual("requirements_mismatch", self._pu_reason(out))

    # -- Cross-state distinctness: the three "gone-looking" states differ --------
    def test_three_states_stay_pairwise_distinct(self):
        r1 = _row("120 Logistics Way", "Reno", "Sam Poe", "sam@brk.com")
        tour = self._classify([
            {"direction": "outbound",
             "content": "Following up on 120 Logistics Way, Reno — could we schedule a tour?"},
            {"direction": "inbound", "from": "sam@brk.com",
             "content": "We can't do tours right now, but it's still on the market."},
        ], r1)

        r2 = _row("4820 Jonestown Rd", "Harrisburg", "Dana Vale", "dana@brk.com")
        unavail = self._classify([
            {"direction": "outbound", "content": "4820 Jonestown Rd?"},
            {"direction": "inbound", "from": "dana@brk.com",
             "content": "4820 Jonestown Rd has been leased — off the market now."},
        ], r2)

        r3 = _row("77 Rivergate Pkwy", "Nashville", "Lee Park", "lee@brk.com")
        nonfit = self._classify([
            {"direction": "outbound", "content": "77 Rivergate Pkwy for a warehouse user?"},
            {"direction": "inbound", "from": "lee@brk.com",
             "content": "It's too office-heavy — not a true warehouse, won't work for your client."},
        ], r3)

        self.assertNotIn("property_unavailable", self._event_types(tour))
        self.assertEqual("tour_unavailable", self._tour_reason(tour))
        self.assertNotEqual("requirements_mismatch", self._pu_reason(unavail))
        self.assertIsNotNone(self._pu_reason(unavail))
        self.assertEqual("requirements_mismatch", self._pu_reason(nonfit))
        # Distinct reasons across the three states.
        reasons = {self._tour_reason(tour), self._pu_reason(unavail), self._pu_reason(nonfit)}
        self.assertEqual(3, len(reasons), f"states collapsed: {reasons}")

    # -- Playbook 2: partial specs → follow-up only for MISSING info + right row -
    def test_partial_specs_write_right_row_and_followup_only_missing(self):
        """Playbook partial_specs_plus_pdf_plus_followup: an available reply giving
        partial facts writes ONLY the provided cells to the CORRECT row anchor, and
        the follow-up gate reports only the still-missing fields (never re-asks a
        filled field, never closes on a 'TBD' placeholder)."""
        rowvals = _row("500 Depot St", "Tulsa", "Jo Reed", "jo@brk.com")
        # Broker provided Total SF + Docks; everything else still missing.
        proposal = {"updates": [
            {"column": "Total SF", "value": "42,000", "confidence": 0.95, "reason": "email"},
            {"column": "Docks", "value": "6", "confidence": 0.95, "reason": "email"},
        ]}
        fake = _FakeSheets()
        with patch("email_automation.ai_processing._sheets_client", return_value=fake), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid-1", "client-1", "sheet-1", HEADER, 7, rowvals, proposal)

        # Right row anchor recorded (row 7, this property — not a neighbor).
        self.assertEqual("500 Depot St, Tulsa", get_row_anchor(rowvals, HEADER))
        self.assertEqual("500 Depot St, Tulsa", result["targetAnchor"])
        # Both provided cells applied, addressed to row 7.
        self.assertEqual(2, len(result["applied"]))
        self.assertEqual(1, len(fake.values_api.batch_update_calls))
        ranges = [d["range"] for d in fake.values_api.batch_update_calls[0]["data"]]
        self.assertTrue(all(r.endswith("7") for r in ranges),
                        f"writes must land on the target row 7, got {ranges}")

        # Follow-up gate: only the genuinely-missing required fields remain.
        after = list(rowvals)
        after[HEADER.index("Total SF")] = "42,000"
        after[HEADER.index("Docks")] = "6"
        missing = check_missing_required_fields(after, HEADER)
        self.assertNotIn("Total SF", missing)
        self.assertNotIn("Docks", missing)
        self.assertIn("Ops Ex /SF", missing)
        self.assertIn("Power", missing)

        # A 'TBD' placeholder must NOT satisfy the gate (no close on a non-answer).
        placeholder_row = list(after)
        placeholder_row[HEADER.index("Ops Ex /SF")] = "TBD"
        self.assertIn("Ops Ex /SF", check_missing_required_fields(placeholder_row, HEADER))

        # When every required field is real data, the gate is empty → no follow-up.
        complete = list(after)
        for f in ("Rent/SF /Yr", "Ops Ex /SF", "Drive Ins", "Ceiling Ht", "Power"):
            complete[HEADER.index(f)] = "1"
        self.assertEqual([], check_missing_required_fields(complete, HEADER))


if __name__ == "__main__":
    unittest.main()
