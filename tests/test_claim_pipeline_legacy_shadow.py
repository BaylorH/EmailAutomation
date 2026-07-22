import unittest
from dataclasses import replace
from pathlib import Path

from email_automation.claim_pipeline.contracts import ActionType, ApprovalClass
from email_automation.claim_pipeline.legacy_shadow import (
    compare_legacy_case,
    project_legacy_proposal,
)
from email_automation.claim_pipeline.legacy_shadow_fixtures import (
    LegacyShadowBindings,
    LegacyShadowProposal,
    load_legacy_shadow_fixture_catalog,
)
from email_automation.claim_pipeline.policy_fixtures import (
    load_policy_fixture_catalog,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures"
SHADOW_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_legacy_shadow_cases.json"
POLICY_FIXTURE_PATH = FIXTURE_ROOT / "claim_pipeline_policy_cases.json"


def _catalog():
    policy = load_policy_fixture_catalog(POLICY_FIXTURE_PATH)
    return load_legacy_shadow_fixture_catalog(
        SHADOW_FIXTURE_PATH,
        policy_catalog=policy,
    )


def _case(case_id):
    return next(case for case in _catalog().cases if case.case_id == case_id)


def _signature(attempt):
    return (
        attempt.entity_key,
        attempt.action_type.value,
        attempt.approval_class.value,
        attempt.qualifier,
    )


class LegacyProjectionTests(unittest.TestCase):
    def test_update_projects_to_bound_current_entity_without_value(self):
        projection = project_legacy_proposal(_case("complete-closeout-aligned"))
        fact_attempts = [
            attempt
            for attempt in projection.attempts
            if attempt.action_type is ActionType.FACT_UPDATE
        ]

        self.assertEqual(
            ["operating_expenses", "rent", "total_sf"],
            sorted(attempt.qualifier for attempt in fact_attempts),
        )
        self.assertEqual({"target"}, {attempt.entity_key for attempt in fact_attempts})
        serialized = projection.to_dict()
        self.assertNotIn("15", str(serialized))
        self.assertNotIn("value", str(serialized).casefold())

    def test_terminal_event_projects_lifecycle_and_row_move(self):
        projection = project_legacy_proposal(_case("aligned-explicit-unavailable"))
        signatures = {_signature(attempt) for attempt in projection.attempts}

        self.assertIn(
            ("target", "followup_freeze", "automatic", "terminal"),
            signatures,
        )
        self.assertIn(
            ("target", "status_transition", "automatic", "terminal"),
            signatures,
        )
        self.assertIn(
            ("target", "row_move", "automatic", "nonviable"),
            signatures,
        )

    def test_new_property_and_redirect_preserve_explicit_entity_scope(self):
        projection = project_legacy_proposal(
            _case("alternate-property-wrong-row-risk")
        )
        signatures = {_signature(attempt) for attempt in projection.attempts}

        self.assertIn(
            (
                "alternate",
                "alternate_property_proposal",
                "human_required",
                "approval",
            ),
            signatures,
        )
        self.assertIn(
            ("alternate", "review_item", "human_required", "new_property"),
            signatures,
        )
        self.assertNotIn(
            ("target", "alternate_property_proposal", "human_required", "approval"),
            signatures,
        )

    def test_wrong_contact_projects_human_recipient_change_and_review(self):
        projection = project_legacy_proposal(_case("redirect-held-for-approval"))
        signatures = {_signature(attempt) for attempt in projection.attempts}

        self.assertIn(
            ("contact", "recipient_change", "human_required", "different"),
            signatures,
        )
        self.assertIn(
            ("contact", "review_item", "human_required", "wrong_contact"),
            signatures,
        )
        self.assertIn(
            ("contact", "status_transition", "automatic", "review"),
            signatures,
        )

    def test_suppressed_response_does_not_project_outbound_draft(self):
        projection = project_legacy_proposal(_case("opt-out-plus-call-held"))

        self.assertNotIn(
            ActionType.OUTBOUND_DRAFT,
            {attempt.action_type for attempt in projection.attempts},
        )

    def test_unsuppressed_response_projects_automatic_outbound_draft(self):
        projection = project_legacy_proposal(
            _case("forwarded-redirect-auto-draft-risk")
        )
        outbound = [
            attempt
            for attempt in projection.attempts
            if attempt.action_type is ActionType.OUTBOUND_DRAFT
        ]

        self.assertEqual(1, len(outbound))
        self.assertIs(ApprovalClass.AUTOMATIC, outbound[0].approval_class)
        self.assertEqual("contact", outbound[0].entity_key)
        self.assertEqual("draft", outbound[0].qualifier)

    def test_optout_and_call_both_remain_visible(self):
        projection = project_legacy_proposal(_case("opt-out-plus-call-held"))
        signatures = {_signature(attempt) for attempt in projection.attempts}

        self.assertIn(
            ("contact", "call_request", "human_required", "call_requested"),
            signatures,
        )
        self.assertIn(
            ("contact", "followup_freeze", "automatic", "terminal"),
            signatures,
        )
        self.assertIn(
            ("contact", "status_transition", "automatic", "review"),
            signatures,
        )
        self.assertIn(
            ("contact", "status_transition", "automatic", "terminal"),
            signatures,
        )

    def test_projection_is_ordered_and_byte_stable(self):
        case = _case("alternate-property-wrong-row-risk")
        first = project_legacy_proposal(case)
        second = project_legacy_proposal(case)

        self.assertEqual(first.projection_digest, second.projection_digest)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            sorted(attempt.attempt_id for attempt in first.attempts),
            [attempt.attempt_id for attempt in first.attempts],
        )


class LegacyComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy_catalog = load_policy_fixture_catalog(POLICY_FIXTURE_PATH)
        cls.policy_cases = {
            case.case_id: case for case in cls.policy_catalog.cases
        }
        cls.shadow_catalog = load_legacy_shadow_fixture_catalog(
            SHADOW_FIXTURE_PATH,
            policy_catalog=cls.policy_catalog,
        )

    def _compare(self, case_id):
        case = next(
            case for case in self.shadow_catalog.cases if case.case_id == case_id
        )
        return compare_legacy_case(case, self.policy_cases[case.policy_case_id])

    def test_every_fixture_matches_its_exact_expected_classification(self):
        for case in self.shadow_catalog.cases:
            with self.subTest(case_id=case.case_id):
                result = compare_legacy_case(
                    case,
                    self.policy_cases[case.policy_case_id],
                )
                self.assertEqual(case.expected.disposition, result.disposition)
                self.assertEqual(case.expected.severity, result.severity)
                self.assertEqual(
                    case.expected.discrepancy_codes,
                    tuple(item.code for item in result.discrepancies),
                )
                self.assertEqual(
                    case.expected.discrepancy_entities,
                    tuple(item.entity_key for item in result.discrepancies),
                )

    def test_wrong_row_mutation_is_a_legacy_release_blocker(self):
        result = self._compare("alternate-property-wrong-row-risk")

        self.assertEqual("legacy_safety_risk", result.disposition)
        self.assertEqual("release_blocker", result.severity)
        self.assertIn(
            "legacy_unplanned_fact_mutation",
            {item.code for item in result.discrepancies},
        )

    def test_tour_only_terminalization_is_not_downgraded_to_row_move_deferral(self):
        result = self._compare("tour-only-terminalization-risk")

        self.assertEqual(
            ("legacy_terminalizes_nonterminal",),
            tuple(item.code for item in result.discrepancies),
        )
        self.assertEqual("release_blocker", result.severity)

    def test_fit_failure_cannot_be_reported_as_market_unavailability(self):
        result = self._compare("fit-market-conflation-risk")

        conflation = next(
            item
            for item in result.discrepancies
            if item.code == "legacy_market_fit_conflation"
        )
        self.assertEqual("legacy_safety_risk", conflation.category)
        self.assertEqual("release_blocker", conflation.severity)

    def test_missing_terminal_freeze_and_status_are_policy_gaps(self):
        result = self._compare("unavailable-without-terminal-event")

        self.assertEqual("new_policy_gap", result.disposition)
        self.assertEqual(
            ("legacy_missing_terminal_freeze", "legacy_missing_terminal_status"),
            tuple(item.code for item in result.discrepancies),
        )

    def test_out_of_office_state_is_visible_as_expected_improvement(self):
        result = self._compare("out-of-office-policy-improvement")

        self.assertEqual("expected_improvement", result.disposition)
        self.assertEqual("info", result.severity)
        self.assertEqual(
            ("policy_adds_waiting_state",),
            tuple(item.code for item in result.discrepancies),
        )

    def test_aligned_redirect_has_no_hidden_difference(self):
        result = self._compare("redirect-held-for-approval")

        self.assertEqual("equivalent", result.disposition)
        self.assertEqual("none", result.severity)
        self.assertEqual((), result.discrepancies)

    def test_all_discrepancies_are_classified(self):
        for case in self.shadow_catalog.cases:
            result = compare_legacy_case(
                case,
                self.policy_cases[case.policy_case_id],
            )
            self.assertNotIn(
                "unclassified_difference",
                {item.code for item in result.discrepancies},
                case.case_id,
            )

    def test_case_result_is_byte_stable(self):
        first = self._compare("conflicting-availability-auto-mutation-risk")
        second = self._compare("conflicting-availability-auto-mutation-risk")

        self.assertEqual(first.result_digest, second.result_digest)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_same_discrepancy_code_is_preserved_for_each_entity(self):
        original = next(
            case
            for case in self.shadow_catalog.cases
            if case.case_id == "split-suite-missing-sibling-update"
        )
        no_legacy_actions = replace(
            original,
            bindings=LegacyShadowBindings(
                current_entity="suite_a",
                event_entities=(),
                recipient_relation="absent",
            ),
            legacy_proposal=LegacyShadowProposal(
                updates=(),
                events=(),
                response_draft=False,
                skip_response=True,
            ),
        )

        result = compare_legacy_case(
            no_legacy_actions,
            self.policy_cases[original.policy_case_id],
        )
        missing_facts = [
            item
            for item in result.discrepancies
            if item.code == "legacy_missing_policy_fact"
        ]

        self.assertEqual(
            ["suite_a", "suite_b"],
            [item.entity_key for item in missing_facts],
        )


if __name__ == "__main__":
    unittest.main()
