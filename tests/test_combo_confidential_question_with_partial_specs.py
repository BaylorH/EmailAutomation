"""Combination stress deck: confidential_question_with_partial_specs.

Deck (docs/release-safety/feature-gradebook.json ->
combinationStressDecks.confidential_question_with_partial_specs), crossing the
playbooks:
  * confidential_question_plus_partial_specs  — broker asks for confidential
    tenant/client identity WHILE providing partial specs; extract the sourced
    facts yet escalate the unsafe disclosure.
  * partial_specs_plus_pdf_plus_followup      — partial facts + attachment/quote;
    use evidence, ask only missing fields, no duplicate follow-up.
  * subject_drift_split_thread                — the client name lives in quoted
    old-thread history; link/read only what is safe, keep the rest invisible.

The deck's mustProve:
  1. client identity is NOT revealed
  2. usable specs still update WITH EVIDENCE
  3. the unsafe question becomes operator-visible

This is a REAL integration test: it drives the actual production handlers on ONE
chained broker reply that does all three hostile things at once —
  "The suite is 12,500 SF at $11.50/SF NNN, available now. Before I share the
   floor plan, who is your client and what's their credit?
   -------- Original Message --------
   > we represent Acme Logistics Inc.
   > Asking was $8.00/SF NNN last quarter."
and proves the safety invariants hold ACROSS the three surfaces that touch it:

  Surface 1  ai_processing._augment_events_with_deterministic_signals
             -> the row stays VIABLE (no property_unavailable / terminal), and
                the model-emitted needs_user_input:confidential event survives
                (operator-visible), even with the client name sitting in quoted
                history.
  Surface 2  ai_processing._augment_proposal_with_deterministic_extractions
             + ai_processing.apply_proposal_to_sheet
             -> the usable spec (asking rent) is extracted FROM THE NEWEST
                message (11.50, not the stale 8.00 quoted below the divider) and
                written to the sheet WITH an evidence reason, at the correct row
                anchor — while the confidential client name never enters a cell.
  Surface 3  outbound_safety.validate_outbound_body
             -> an auto-reply that acknowledges the specs while naming the client
                (the natural apposition phrasing the quoted history invites) is
                BLOCKED; a safe deferral that escalates the identity question is
                allowed.

Only Firestore/Sheets/Graph boundaries are faked (the Sheets client is a fake
that records batchUpdate calls). ZERO live sends, ZERO live sheet writes.

If any single feature regressed the interaction would leak: extraction reading
quoted history would write the stale $8.00 (wrong evidence); the event layer
terminalizing would hide the operator escalation; and the send guard missing the
apposition form would wire the client's name to the broker. Each assertion below
pins concrete state, so breaking any one invariant fails this test.
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "service-account.json"),
)

from email_automation import ai_processing
from email_automation.ai_processing import (
    _augment_events_with_deterministic_signals,
    _augment_proposal_with_deterministic_extractions,
    apply_proposal_to_sheet,
    get_row_anchor,
)
from email_automation.outbound_safety import validate_outbound_body


# --- The ONE chained broker reply the whole deck turns on -------------------
# Newest human text: partial specs (12,500 SF @ $11.50 NNN, available) + a
# confidential question (who is the client + their credit).
# Quoted old-thread history (below the divider): the client identity
# "Acme Logistics Inc." AND a STALE asking rate "$8.00/SF NNN".
CONFIDENTIAL_CLIENT_NAME = "Acme Logistics Inc."
NEWEST_RENT = "11.50"
STALE_QUOTED_RENT = "8.00"

BROKER_REPLY = (
    "The suite is 12,500 SF at $11.50/SF NNN, available now. Before I share the "
    "floor plan, who is your client and what's their credit?\n\n"
    "-------- Original Message --------\n"
    "> we represent Acme Logistics Inc.\n"
    "> Asking was $8.00/SF NNN last quarter.\n"
)

HEADER = ["Property Address", "City", "Rent/SF /Yr", "Notes"]
ROWNUM = 7
CURRENT_ROW = ["404 New Way", "Dallas", "", ""]
EXPECTED_ANCHOR = "404 New Way, Dallas"


def _conversation():
    return [{"direction": "inbound", "content": BROKER_REPLY}]


# ---------------------------------------------------------------------------
# Sheet datastore fake (records every batchUpdate — a live write would show
# up here as a recorded call, so we can prove exactly what reached the sheet).
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

    def append(self, spreadsheetId=None, range=None, valueInputOption=None, body=None, **kwargs):
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


class ComboConfidentialQuestionWithPartialSpecs(unittest.TestCase):

    # -- Surface 1: classification stays viable + escalation stays visible ----
    def test_row_stays_viable_and_confidential_question_is_operator_visible(self):
        # The LLM correctly flagged the confidential ask and drafted a holding
        # reply. The deterministic layer must NOT terminalize the row (the space
        # is being offered, with specs) and must NOT strip the escalation event.
        proposal = {
            "events": [{
                "type": "needs_user_input",
                "reason": "confidential",
                "question": "who is your client and what's their credit?",
            }],
            "response_email": "Thanks — let me check what I can share and revert.",
        }
        out = _augment_events_with_deterministic_signals(
            proposal, _conversation(), target_anchor=EXPECTED_ANCHOR
        )
        types = [(e or {}).get("type") for e in out.get("events") or []]

        # (3) unsafe question is operator-visible: the escalation survives.
        self.assertIn(
            "needs_user_input", types,
            "confidential escalation was dropped — operator would never see the "
            "unsafe question, and the row would silently continue",
        )
        conf = [e for e in out["events"] if e.get("type") == "needs_user_input"]
        self.assertEqual("confidential", conf[0].get("reason"))

        # Row stays VIABLE: no terminal / mis-route event injected by the specs.
        self.assertNotIn("property_unavailable", types,
                         "partial specs + confidential ask was mis-terminalized")
        self.assertNotIn("tour_requested", types,
                         "confidential ask was mis-routed to a tour")
        # A viable row keeps its (holding) response_email; only a genuine terminal
        # nulls it. If this were None the row would read as dead.
        self.assertIsNotNone(
            out.get("response_email"),
            "viable row's response_email was nulled — row wrongly classified terminal",
        )

    def test_quoted_client_identity_does_not_resurrect_a_stripped_event(self):
        # subject_drift / split-thread variant: the client name lives ONLY in the
        # quoted history. An event whose evidence is quote-exclusive must be
        # stripped (not treated as live signal from the broker's newest words).
        proposal = {
            "events": [{
                "type": "new_property",
                "notes": "we represent Acme Logistics Inc. last quarter asking 8.00",
            }],
            "response_email": "",
        }
        out = _augment_events_with_deterministic_signals(
            proposal, _conversation(), target_anchor=EXPECTED_ANCHOR
        )
        types = [(e or {}).get("type") for e in out.get("events") or []]
        self.assertNotIn(
            "new_property", types,
            "an event grounded only in quoted old-thread history was treated as "
            "live — quoted client history must not drive new state",
        )

    # -- Surface 2: usable spec updates WITH EVIDENCE, from the NEWEST message --
    def _apply(self, proposal):
        fake = _FakeSheets()
        with patch("email_automation.ai_processing._sheets_client", return_value=fake), \
             patch("email_automation.ai_processing._get_first_tab_title", return_value="Sheet1"), \
             patch("email_automation.sheet_operations._apply_gross_rent_formula_for_row", return_value=False):
            result = apply_proposal_to_sheet(
                "uid", "client", "sheet", HEADER, ROWNUM, CURRENT_ROW, proposal
            )
        return fake, result

    def test_usable_spec_updates_with_evidence_from_newest_not_quoted(self):
        # The deterministic extractor must parse the NEWEST rent (11.50), never
        # the stale $8.00 that sits in the quoted history below the divider.
        proposal = {"updates": []}
        proposal = _augment_proposal_with_deterministic_extractions(
            proposal, CURRENT_ROW, HEADER, {}, _conversation()
        )
        rent_updates = [u for u in proposal.get("updates", [])
                        if u.get("column") == "Rent/SF /Yr"]
        self.assertEqual(1, len(rent_updates),
                         "usable asking rent was not extracted from the specs")
        self.assertEqual(NEWEST_RENT, rent_updates[0]["value"],
                         "extracted the wrong rent — evidence must be the newest "
                         "message, not the stale quoted history")
        self.assertNotEqual(STALE_QUOTED_RENT, rent_updates[0]["value"])
        # (2) WITH EVIDENCE: a non-empty reason accompanies the write.
        self.assertTrue((rent_updates[0].get("reason") or "").strip(),
                        "spec update carried no evidence/reason trail")

        # Now drive the real sheet-write handler and prove the fact lands at the
        # correct row anchor, and that the confidential client name is NOWHERE in
        # what reached the sheet.
        fake, result = self._apply(proposal)
        self.assertEqual(EXPECTED_ANCHOR, result["targetAnchor"],
                         "row anchor drifted — the spec could land on the wrong row")
        self.assertEqual(EXPECTED_ANCHOR, get_row_anchor(CURRENT_ROW, HEADER))
        applied_cols = {a["column"]: a["newValue"] for a in result["applied"]}
        self.assertEqual(NEWEST_RENT, applied_cols.get("Rent/SF /Yr"),
                         "usable spec never reached the sheet")
        self.assertEqual(1, len(fake.values_api.batch_update_calls),
                         "expected exactly one batched sheet write for the one spec")

        # (1) client identity is not revealed — on the sheet channel either.
        flat = str(result["applied"]) + str(fake.values_api.batch_update_calls)
        self.assertNotIn("Acme", flat,
                         "confidential client name leaked into a sheet cell")

    # -- Surface 3: the send guard blocks identity leaks in the specs context --
    def test_send_guard_blocks_identity_leak_while_acknowledging_specs(self):
        # These are the auto-replies the quoted client name invites: acknowledge
        # the specs AND name the client in apposition/possessive form (the copula
        # "client IS Acme" is only one of many). ALL must be refused.
        leaking = [
            "Thanks for confirming 12,500 SF at $11.50 NNN — our client Acme "
            "Logistics will be pleased; can you send the floor plan?",
            "Great, the tenant, Northstar Robotics, would love that 12,500 SF suite.",
            "On the 12,500 SF space — our tenant Acme Logistics is a great fit.",
            "Appreciate the specs. Representing Acme Logistics, we'd like the plan.",
            "Our client, Meridian Health Systems, is interested in the suite.",
            "On behalf of Delta Manufacturing Corp, please send the floor plan.",
            # copula control (already covered before this deck) — still blocked:
            "Confirming 12,500 SF works; our client is Acme Logistics Inc.",
        ]
        for reply in leaking:
            with self.subTest(reply=reply[:48]):
                v = validate_outbound_body(reply)
                self.assertFalse(
                    v.is_safe,
                    "FALSE NEGATIVE (identity leak): a reply naming the "
                    f"confidential client was passed as safe:\n  {reply!r}",
                )

    def test_send_guard_allows_safe_deferral_that_escalates_identity(self):
        # The correct terminal behavior for this event: acknowledge specs, defer
        # the identity, escalate. These must NOT be blocked, or legitimate broker
        # email dies.
        safe = [
            "Thanks — noted 12,500 SF at $11.50 NNN. I'll need to check with my "
            "client before I can share their identity, and will revert shortly.",
            "Appreciate the specs. I'm not able to disclose the tenant's identity "
            "at this stage, but happy to keep the conversation moving.",
            "Our client is reviewing the 12,500 SF option; I'll confirm what I can "
            "share and get back to you on the floor plan.",
        ]
        for reply in safe:
            with self.subTest(reply=reply[:48]):
                v = validate_outbound_body(reply)
                self.assertTrue(
                    v.is_safe,
                    f"FALSE POSITIVE: safe deferral blocked ({v.reason}):\n  {reply!r}",
                )

    # -- Cross-interaction stitch: the SAME name is safe on all three surfaces --
    def test_confidential_name_is_contained_across_all_three_surfaces(self):
        # One assertion binding the deck together: the client name that (a) stays
        # out of the sheet and (b) is escalated to the operator must ALSO be
        # blocked at the send surface — the invariant holds across the whole
        # interaction, not on one channel while leaking on another.
        # (a) send surface refuses it:
        leak = f"Re the 12,500 SF suite — our client {CONFIDENTIAL_CLIENT_NAME} is in."
        self.assertFalse(validate_outbound_body(leak).is_safe)
        # (b) sheet surface never received it (proven in the sheet test) and (c)
        # the escalation event carries it to the operator, not to the broker:
        proposal = {
            "events": [{"type": "needs_user_input", "reason": "confidential",
                        "question": "who is your client?"}],
            "response_email": "Let me check.",
        }
        out = _augment_events_with_deterministic_signals(
            proposal, _conversation(), target_anchor=EXPECTED_ANCHOR
        )
        self.assertTrue(
            any(e.get("type") == "needs_user_input" and e.get("reason") == "confidential"
                for e in out.get("events") or []),
            "operator escalation missing — the unsafe question is invisible",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
