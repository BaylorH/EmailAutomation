"""Production-pipeline regressions for current-target drive-in evidence.

Only the OpenAI response is faked.  ``propose_sheet_updates`` and every
deterministic reconciliation guard are the same code used by the inbound worker.

STATUS 2026-08-19: green (Ran 21 / OK). It was previously red at 45 failures
and 20 errors, and the earlier note here read that as a semantic gap across the
reconciliation rules. It was not. The fixture's ``CONFIG["mappings"]`` keyed the
address column as ``"address"``, which is not a canonical field — the canonical
key is ``property_address``, as every sibling fixture in this suite writes it.
``propose_sheet_updates`` validates the contract before it does anything else
and returns ``None`` on an unsafe one, printing

    Refusing unsafe columnConfig: columnConfig.mappings contains unknown canonical fields

so all 21 methods asserted against ``None`` and the module exercised no
reconciliation logic at all. It had that typo in its first commit, so it had
never once run the pipeline; the four ``fix:`` commits recorded against it were
landed with their regression tests inert.

The suite is now live, not merely green: mutating
``_current_target_drive_evidence`` to yield no evidence kills 55 subtests, and
mutating ``_detect_target_terminal_reason`` to yield no reason kills the
terminal-precedence method that survives the first mutation. Treat 21/OK as the
baseline and investigate any change from it.
"""

import json
import os
import unittest
from unittest import mock


os.environ.setdefault("E2E_TEST_MODE", "true")
os.environ.setdefault(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "service-account.json",
    ),
)

from email_automation import ai_processing as ai


HEADER = [
    "Property Address",
    "Total SF",
    "Rent/SF/Yr",
    "Ops Ex / SF",
    "Ceiling Ht",
    "Drive Ins",
    "Docks",
    "Power",
]
CONFIG = {
    "mappings": {
        "property_address": "Property Address",
        "total_sf": "Total SF",
        "rent_sf_yr": "Rent/SF/Yr",
        "ops_ex_sf": "Ops Ex / SF",
        "ceiling_ht": "Ceiling Ht",
        "drive_ins": "Drive Ins",
        "docks": "Docks",
        "power": "Power",
    },
    "extractionFields": [
        "total_sf",
        "rent_sf_yr",
        "ops_ex_sf",
        "ceiling_ht",
        "drive_ins",
        "docks",
        "power",
    ],
    "requiredFields": [],
    "formulaFields": [],
    "neverRequest": [],
    "customFields": {},
}


def _update(column, value):
    return {
        "column": column,
        "value": str(value),
        "confidence": 0.97,
        "reason": "semantic model",
    }


def _conversation(*bodies):
    messages = []
    for index, body in enumerate(bodies):
        messages.append({
            "direction": "inbound" if index % 2 == 0 else "outbound",
            "content": body,
        })
    return messages


def _run(
    body,
    *,
    updates=None,
    events=None,
    response_email="Hi,\n\nThanks for the details.",
    target="100 Main St",
    conversation=None,
    pdf_manifest=None,
    config=None,
):
    proposal = {
        "updates": list(updates or []),
        "events": list(events or []),
        "response_email": response_email,
        "notes": "",
    }
    fake_response = mock.Mock()
    fake_response.output_text = json.dumps(proposal)
    fake_response.usage = None
    fake_response.id = "resp-zero-drive-state-machine"
    fake_client = mock.Mock()
    fake_client.responses.create.return_value = fake_response

    with mock.patch.object(ai, "client", fake_client):
        return ai.propose_sheet_updates(
            uid="internal-proof",
            client_id="zero-drive-state-machine",
            email="bp21harrison@gmail.com",
            sheet_id="proof-sheet",
            header=HEADER,
            rownum=3,
            rowvals=[target, "", "", "", "", "", "", ""],
            thread_id="proof-thread",
            conversation=conversation or [{"direction": "inbound", "content": body}],
            pdf_manifest=pdf_manifest,
            column_config=config if config is not None else CONFIG,
            dry_run=True,
        )


def _value(proposal, column="Drive Ins"):
    update = ai._proposal_update_for_column(proposal, column)
    return None if update is None else str(update.get("value"))


def _events(proposal, event_type):
    return [
        event
        for event in proposal.get("events", [])
        if event.get("type") == event_type
    ]


class ZeroDriveStateMachineTests(unittest.TestCase):
    def test_live_complete_reply_keeps_all_facts_and_corrects_drive_ins_to_zero(self):
        updates = [
            _update("Total SF", "18750"),
            _update("Rent/SF/Yr", "14.10"),
            _update("Ops Ex / SF", "3.90"),
            _update("Ceiling Ht", "24"),
            _update("Drive Ins", "1"),
            _update("Docks", "2"),
            _update("Power", "400A 480V"),
        ]
        result = _run(
            "100 Main St remains available. It has no grade-level drive-ins, "
            "2 docks, 18,750 SF at $14.10/SF NNN plus $3.90/SF OpEx, 24-foot "
            "clear height, and 400A 480V power. The optional flyer is unavailable.",
            updates=updates,
            events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
        )

        self.assertEqual("0", _value(result))
        self.assertEqual(
            {"Total SF", "Rent/SF/Yr", "Ops Ex / SF", "Ceiling Ht", "Drive Ins", "Docks", "Power"},
            {update["column"] for update in result["updates"]},
        )
        self.assertEqual([], _events(result, "property_unavailable"))

    def test_jpr03_zero_dock_and_ramp_is_review_not_terminal(self):
        result = _run(
            "100 Main St has no drive-in door and one loading dock. The dock "
            "can potentially be ramped for drive-in access. The unit is 7,753 SF.",
            updates=[
                _update("Drive Ins", "1"),
                _update("Docks", "1"),
                _update("Total SF", "7753"),
            ],
            events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
        )

        self.assertEqual("0", _value(result))
        self.assertEqual("1", _value(result, "Docks"))
        self.assertEqual("7753", _value(result, "Total SF"))
        self.assertEqual([], _events(result, "property_unavailable"))
        self.assertTrue(_events(result, "needs_user_input"))
        self.assertIsNone(result["response_email"])

    def test_rampability_review_removes_contradictory_close_event(self):
        result = _run(
            "100 Main St has no drive-in door and one loading dock. The dock "
            "can potentially be ramped for drive-in access. The unit is 7,753 SF.",
            updates=[
                _update("Drive Ins", "1"),
                _update("Docks", "1"),
                _update("Total SF", "7753"),
            ],
            events=[
                {"type": "property_unavailable", "reason": "requirements_mismatch"},
                {"type": "close_conversation", "notes": "all_info_gathered"},
            ],
            response_email="Thanks for confirming the property details.",
        )

        self.assertEqual("0", _value(result))
        self.assertEqual([], _events(result, "property_unavailable"))
        review_events = _events(result, "needs_user_input")
        self.assertEqual(1, len(review_events))
        self.assertEqual("drive_access_requires_review", review_events[0]["reason"])
        self.assertEqual([], _events(result, "close_conversation"))
        self.assertIsNone(result["response_email"])

    def test_rampability_review_yields_to_independent_terminal_close_events(self):
        cases = (
            (
                "exclusive_with_another",
                "We're going exclusive with another tenant rep.",
            ),
            (
                "deal_pending",
                "We're already in negotiations with another tenant and expect to sign next week.",
            ),
            (
                "natural_end",
                "Thanks for reaching out, and good luck with your search.",
            ),
            (
                "not_a_fit",
                "We can't help right now because we're not a fit to work together.",
            ),
        )
        for close_reason, terminal_text in cases:
            with self.subTest(close_reason=close_reason):
                result = _run(
                    "100 Main St has no drive-ins and the dock could be ramped. "
                    + terminal_text,
                    updates=[_update("Drive Ins", "1")],
                    events=[
                        {"type": "property_unavailable", "reason": "requirements_mismatch"},
                        {"type": "close_conversation", "notes": close_reason},
                        {
                            "type": "needs_user_input",
                            "reason": "drive_access_requires_review",
                            "question": "Review the potentially rampable dock.",
                        },
                    ],
                    response_email="Thanks for confirming the property details.",
                )

                self.assertEqual("0", _value(result))
                self.assertEqual([], _events(result, "property_unavailable"))
                self.assertEqual([], _events(result, "needs_user_input"))
                close_events = _events(result, "close_conversation")
                self.assertEqual(1, len(close_events))
                self.assertEqual(close_reason, close_events[0]["notes"])
                self.assertIsNone(result["response_email"])

    def test_last_trusted_target_assertion_wins_corrections(self):
        cases = (
            (
                "100 Main St has no drive-ins. Correction: it has one drive-in door and remains available.",
                [_update("Drive Ins", "1")],
                "1",
            ),
            (
                "100 Main St has one drive-in. Correction: it has no drive-ins and remains available.",
                [_update("Drive Ins", "1")],
                "0",
            ),
            (
                "100 Main St has no drive-ins, correction: it has one drive-in door.",
                [_update("Drive Ins", "1")],
                "1",
            ),
            (
                "100 Main St has one drive-in—correction: it has no drive-ins.",
                [_update("Drive Ins", "1")],
                "0",
            ),
            (
                "100 Main St has no drive-ins—scratch that, it has two drive-ins.",
                [_update("Drive Ins", "2")],
                "2",
            ),
            (
                "100 Main St has no drive-ins, I mean two drive-ins.",
                [_update("Drive Ins", "2")],
                "2",
            ),
        )
        for body, updates, expected in cases:
            with self.subTest(body=body):
                result = _run(
                    body,
                    updates=updates,
                    events=[{"type": "property_unavailable", "reason": "no_drive_ins"}],
                )
                self.assertEqual(expected, _value(result))
                self.assertEqual([], _events(result, "property_unavailable"))

    def test_positive_ten_is_not_misread_as_zero(self):
        result = _run(
            "100 Main St has 10 grade-level drive-in doors and remains available.",
            updates=[_update("Drive Ins", "10")],
        )
        self.assertEqual("10", _value(result))

    def test_noncurrent_or_requirement_only_zero_is_never_written(self):
        bodies = (
            "Does 100 Main St have no drive-ins?",
            "If 100 Main St has no drive-ins, let me know.",
            "Under the proposed layout, 100 Main St would have no drive-ins.",
            "100 Main St has no drive-ins under the proposed layout.",
            "I do not know whether 100 Main St has any drive-ins.",
            "Information about whether 100 Main St has drive-ins is unavailable.",
            "Our client requires no fewer than two drive-ins at 100 Main St.",
            "Our client requires at least zero drive-ins at 100 Main St.",
            "100 Main St does not have zero drive-ins.",
        )
        for body in bodies:
            with self.subTest(body=body):
                result = _run(
                    body,
                    updates=[_update("Drive Ins", "0")],
                    events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
                )
                self.assertIsNone(_value(result))
                self.assertEqual([], _events(result, "property_unavailable"))

    def test_stale_plan_drive_counts_do_not_override_current_target_facts(self):
        cases = (
            (
                "100 Main St currently has two drive-ins. The old plan showed no drive-ins.",
                [_update("Drive Ins", "2")],
                "2",
            ),
            (
                "The prior proposal showed no drive-ins. 100 Main St now has two drive-ins.",
                [_update("Drive Ins", "2")],
                "2",
            ),
            (
                "The old brick building at 100 Main St has no drive-ins.",
                [_update("Drive Ins", "1")],
                "0",
            ),
            (
                "100 Main St has two drive-ins now. Previously, it had no drive-ins.",
                [_update("Drive Ins", "2")],
                "2",
            ),
            (
                "100 Main St has two drive-ins now. Before renovation, it had no "
                "drive-ins. The flyer is attached.",
                [_update("Drive Ins", "2")],
                "2",
            ),
            (
                "100 Main St had no drive-ins before renovation. The renovated "
                "property remains available.",
                [_update("Drive Ins", "0")],
                None,
            ),
        )
        for body, updates, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(expected, _value(_run(body, updates=updates)))

    def test_collateral_missing_drive_count_is_not_a_target_zero(self):
        for body in (
            "The flyer has no drive-in count. 100 Main St remains available.",
            "The website has no drive-in data. 100 Main St remains available.",
        ):
            with self.subTest(body=body):
                self.assertIsNone(
                    _value(_run(body, updates=[_update("Drive Ins", "0")]))
                )

        self.assertEqual(
            "0",
            _value(_run(
                "100 Main St has a parking lot and no drive-ins.",
                updates=[_update("Drive Ins", "1")],
            )),
        )

    def test_negated_positive_count_does_not_validate_model_update(self):
        bodies = (
            "100 Main St does not have three drive-ins.",
            "100 Main St is not equipped with three drive-ins.",
            "100 Main St would need three drive-ins.",
        )
        for body in bodies:
            with self.subTest(body=body):
                result = _run(body, updates=[_update("Drive Ins", "3")])
                self.assertIsNone(_value(result))

        result = _run(
            "100 Main St has two drive-ins, not zero drive-ins, and remains available.",
            updates=[_update("Drive Ins", "2")],
        )
        self.assertEqual("2", _value(result))

    def test_disabled_drive_extraction_never_writes_zero(self):
        disabled = {
            **CONFIG,
            "extractionFields": [
                field for field in CONFIG["extractionFields"] if field != "drive_ins"
            ],
        }
        result = _run(
            "100 Main St has no drive-ins and remains available.",
            updates=[_update("Drive Ins", "0")],
            config=disabled,
        )
        self.assertIsNone(_value(result))

    def test_positive_minimum_plus_current_zero_fails_closed_for_review(self):
        bodies = (
            "Our client requires at least two drive-ins. 100 Main St has no drive-ins but remains available.",
            "The drive-in minimum is two. 100 Main St has no drive-ins.",
            "100 Main St has no drive-ins; the client minimum is two.",
            "100 Main St has no drive-ins; we need two for the client.",
            "Minimum drive-ins: two. 100 Main St has no drive-ins.",
            "100 Main St has no drive-ins; the client requires two.",
        )
        for body in bodies:
            with self.subTest(body=body):
                result = _run(
                    body,
                    updates=[_update("Drive Ins", "0")],
                    events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
                )
                self.assertEqual("0", _value(result))
                self.assertEqual([], _events(result, "property_unavailable"))
                self.assertTrue(_events(result, "needs_user_input"))
                self.assertIsNone(result["response_email"])

    def test_explicit_target_nonfit_and_unavailability_remain_terminal(self):
        cases = (
            (
                "100 Main St has no drive-ins and is not a fit for the client.",
                "requirements_mismatch",
            ),
            (
                "100 Main St has three drive-ins but is not a fit for the client.",
                "requirements_mismatch",
            ),
            (
                "100 Main St has three drive-ins but is too office-heavy for the client.",
                "requirements_mismatch",
            ),
            (
                "100 Main St has three drive-ins but is not a warehouse.",
                "requirements_mismatch",
            ),
            ("100 Main St is no longer available.", "no_longer_available"),
        )
        for body, reason in cases:
            with self.subTest(body=body):
                updates = [_update("Drive Ins", "3")] if "three drive-ins" in body else []
                result = _run(body, updates=updates, events=[])
                unavailable = _events(result, "property_unavailable")
                self.assertEqual(1, len(unavailable))
                self.assertEqual(reason, unavailable[0]["reason"])

    def test_competing_property_nonfit_does_not_terminalize_positive_target(self):
        bodies = (
            "100 Main St has three drive-ins and remains available. A different property is not a fit.",
            "100 Main St has three drive-ins and remains available. A different property is too office-heavy.",
        )
        for body in bodies:
            with self.subTest(body=body):
                result = _run(
                    body,
                    updates=[_update("Drive Ins", "3")],
                    events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
                )
                self.assertEqual("3", _value(result))
                self.assertEqual([], _events(result, "property_unavailable"))

    def test_drive_evidence_is_bound_to_target_not_other_subjects(self):
        cases = (
            (
                "A different property has no drive-ins. 100 Main St remains available.",
                [_update("Drive Ins", "0")],
                None,
            ),
            (
                "The other one has no drive-ins. 100 Main St remains available.",
                [_update("Drive Ins", "0")],
                None,
            ),
            (
                "The parking lot has no drive-ins. 100 Main St remains available.",
                [_update("Drive Ins", "0")],
                None,
            ),
            (
                "200 Oak Rd is another property. The property has no drive-ins. "
                "100 Main St remains available.",
                [_update("Drive Ins", "0")],
                None,
            ),
            (
                "100 Main St has no drive-ins. The building at 200 Oak Rd has three.",
                [_update("Drive Ins", "3")],
                "0",
            ),
            (
                "200 Oak Rd has three drive-ins. 100 Main St remains available.",
                [_update("Drive Ins", "3")],
                None,
            ),
            (
                "200 Oak Rd has three drive-ins. 100 Main St has three drive-ins.",
                [_update("Drive Ins", "3")],
                "3",
            ),
        )
        for body, updates, expected in cases:
            with self.subTest(body=body):
                result = _run(body, updates=updates)
                self.assertEqual(expected, _value(result))

    def test_drive_reason_alias_cleanup_is_bounded_and_preserves_owner_event(self):
        aliases = (
            "requirements_mismatch",
            "No drive-in doors",
            "missing drive-ins",
            "no_grade_level_access",
            "zero drive ins",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                result = _run(
                    "100 Main St has no drive-ins and remains available.",
                    updates=[_update("Drive Ins", "1")],
                    events=[
                        {"type": "property_unavailable", "reason": alias},
                        {"type": "property_unavailable", "reason": "owner_withdrew"},
                    ],
                )
                self.assertEqual("0", _value(result))
                self.assertEqual(
                    ["owner_withdrew"],
                    [event["reason"] for event in _events(result, "property_unavailable")],
                )

    def test_optional_collateral_does_not_mask_or_create_target_terminal(self):
        harmless = (
            "The optional flyer is unavailable. 100 Main St remains available.",
            "The brochure link is no longer available, but 100 Main St is still available.",
            "The flyer isn't available, but 100 Main St remains available.",
            "The optional flyer for 100 Main St is unavailable.",
            "The brochure link for 100 Main St is no longer available.",
        )
        for body in harmless:
            with self.subTest(body=body):
                result = _run(
                    body,
                    events=[{"type": "property_unavailable", "reason": "no_longer_available"}],
                    response_email="Unfortunately, this property is no longer available.",
                )
                self.assertEqual([], _events(result, "property_unavailable"))
                self.assertIsNone(result["response_email"])

        result = _run(
            "The flyer is unavailable. 200 Oak Rd is another property. The property "
            "has been leased. 100 Main St remains available.",
            events=[{"type": "property_unavailable", "reason": "been_leased"}],
            response_email="Unfortunately, this property has been leased.",
        )
        self.assertEqual([], _events(result, "property_unavailable"))
        self.assertIsNone(result["response_email"])

        for body in (
            "200 Oak Rd has no drive-ins and has been leased. 100 Main St remains available.",
            "The other property has no drive-ins and has been leased. "
            "100 Main St remains available.",
        ):
            with self.subTest(body=body):
                result = _run(
                    body,
                    updates=[_update("Drive Ins", "0")],
                    events=[{"type": "property_unavailable", "reason": "been_leased"}],
                    response_email="Unfortunately, this property has been leased.",
                )
                self.assertIsNone(_value(result))
                self.assertEqual([], _events(result, "property_unavailable"))
                self.assertIsNone(result["response_email"])

        result = _run(
            "The flyer is unavailable because 100 Main St is no longer available.",
            events=[],
        )
        unavailable = _events(result, "property_unavailable")
        self.assertEqual(1, len(unavailable))
        self.assertEqual("no_longer_available", unavailable[0]["reason"])

        result = _run(
            "The flyer for 100 Main St is unavailable because 100 Main St itself has been leased.",
            events=[],
        )
        unavailable = _events(result, "property_unavailable")
        self.assertEqual(1, len(unavailable))
        self.assertEqual("been_leased", unavailable[0]["reason"])

        result = _run(
            "The flyer is unavailable, but the property has been leased.",
            events=[],
        )
        unavailable = _events(result, "property_unavailable")
        self.assertEqual(1, len(unavailable))
        self.assertEqual("been_leased", unavailable[0]["reason"])
        self.assertIsNone(result["response_email"])

        result = _run(
            "The property has been leased, and the flyer is unavailable.",
            events=[],
        )
        unavailable = _events(result, "property_unavailable")
        self.assertEqual(1, len(unavailable))
        self.assertEqual("been_leased", unavailable[0]["reason"])
        self.assertIsNone(result["response_email"])

    def test_only_fresh_unquoted_correction_controls_drive_value(self):
        conversation = [
            {"direction": "inbound", "content": "100 Main St has one drive-in."},
            {"direction": "outbound", "content": "Thanks. I recorded one drive-in."},
            {
                "direction": "inbound",
                "content": (
                    "Correction: 100 Main St has no drive-ins and remains available.\n\n"
                    "On Thu, Aug 6, 2026 at 9:00 AM SiteSift wrote:\n"
                    "> Thanks. I recorded one drive-in."
                ),
            },
        ]
        result = _run(
            conversation[-1]["content"],
            updates=[_update("Drive Ins", "1")],
            events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
            conversation=conversation,
        )
        self.assertEqual("0", _value(result))
        self.assertEqual([], _events(result, "property_unavailable"))

    def test_same_clause_subjects_and_remediation_remain_evidence_local(self):
        cases = (
            (
                "100 Main St has no drive-ins and 200 Oak Rd has three drive-ins.",
                [_update("Drive Ins", "3")],
                "0",
                False,
            ),
            (
                "It has ample parking and no drive-ins, and remains available.",
                [_update("Drive Ins", "1")],
                "0",
                False,
            ),
            (
                "100 Main St has no drive-ins and the dock could be ramped for access.",
                [_update("Drive Ins", "1")],
                "0",
                True,
            ),
            (
                "100 Main St has no drive-ins and the dock could not be ramped, so it will not work.",
                [_update("Drive Ins", "1")],
                "0",
                False,
            ),
        )
        for body, updates, expected, review in cases:
            with self.subTest(body=body):
                result = _run(
                    body,
                    updates=updates,
                    events=[] if "could not" in body else [
                        {"type": "property_unavailable", "reason": "requirements_mismatch"}
                    ],
                )
                self.assertEqual(expected, _value(result))
                self.assertEqual(review, bool(_events(result, "needs_user_input")))
                if "could not" in body:
                    self.assertTrue(_events(result, "property_unavailable"))
                    self.assertIsNone(result["response_email"])
                else:
                    self.assertEqual([], _events(result, "property_unavailable"))

    def test_drive_counts_from_attachments_require_target_provenance(self):
        cases = (
            (
                "All current loading details are in the attached flyer.",
                "CURRENT FLYER: one drive-in door.",
                [_update("Drive Ins", "1")],
                "1",
            ),
            (
                "All current loading details are in the attached flyer.",
                "100 Main St: one drive-in door.",
                [_update("Drive Ins", "1")],
                "1",
            ),
            (
                "All current loading details are in the attached flyer.",
                "200 Oak Rd: three drive-in doors.",
                [_update("Drive Ins", "3")],
                None,
            ),
        )
        for body, flyer, updates, expected in cases:
            with self.subTest(flyer=flyer):
                result = _run(
                    body,
                    updates=updates,
                    pdf_manifest=[{
                        "name": "loading-facts.pdf",
                        "text": flyer,
                        "method": "production-replay",
                    }],
                )
                self.assertEqual(expected, _value(result))

        result = _run(
            "100 Main St remains available. Current loading details are in the attached flyer.",
            updates=[_update("Drive Ins", "1")],
            events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
            response_email="Unfortunately, this property is not a fit.",
            pdf_manifest=[{
                "name": "loading-facts.pdf",
                "text": "100 Main St: no drive-ins.",
                "method": "production-replay",
            }],
        )
        self.assertEqual("0", _value(result))
        self.assertEqual([], _events(result, "property_unavailable"))
        self.assertIsNone(result["response_email"])

        result = _run(
            "The client requires at least two drive-ins. Current loading details "
            "are in the attached flyer.",
            updates=[_update("Drive Ins", "1")],
            events=[],
            response_email="Thanks for the details.",
            pdf_manifest=[{
                "name": "loading-facts.pdf",
                "text": "100 Main St has no drive-ins.",
                "method": "production-replay",
            }],
        )
        self.assertEqual("0", _value(result))
        self.assertTrue(_events(result, "needs_user_input"))
        self.assertIsNone(result["response_email"])

    def test_same_unit_current_zero_and_positive_minimum_is_reviewed(self):
        result = _run(
            "100 Main St has no drive-ins, below the client's minimum of two drive-ins.",
            updates=[_update("Drive Ins", "1")],
            events=[{"type": "property_unavailable", "reason": "requirements_mismatch"}],
        )
        self.assertEqual("0", _value(result))
        self.assertEqual([], _events(result, "property_unavailable"))
        self.assertTrue(_events(result, "needs_user_input"))
        self.assertIsNone(result["response_email"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
