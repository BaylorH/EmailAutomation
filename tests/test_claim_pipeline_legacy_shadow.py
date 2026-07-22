import unittest
from pathlib import Path

from email_automation.claim_pipeline.contracts import ActionType, ApprovalClass
from email_automation.claim_pipeline.legacy_shadow import (
    project_legacy_proposal,
)
from email_automation.claim_pipeline.legacy_shadow_fixtures import (
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


if __name__ == "__main__":
    unittest.main()
