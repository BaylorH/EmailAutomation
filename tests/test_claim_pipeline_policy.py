import unittest
from pathlib import Path

from email_automation.claim_pipeline.contracts import (
    ActorRole,
    ApprovalClass,
    CampaignContract,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    ContractAuthority,
    ConversationState,
    EntityRef,
    EntityType,
    ExecutionScope,
)
from email_automation.claim_pipeline.policy import (
    PolicyEvaluationRequest,
    evaluate_policy,
)
from email_automation.claim_pipeline.policy_fixtures import (
    load_policy_fixture_catalog,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "claim_pipeline_policy_cases.json"
)


def _build_case(case):
    contract = CampaignContract.create(
        tenant_id="tenant-1",
        client_id="client-1",
        campaign_id="campaign-1",
        version=int(case.contract.get("version", 1)),
        required_fields=tuple(case.contract.get("requiredFields", ())),
        hard_requirements=dict(case.contract.get("hardRequirements", {})),
        soft_preferences=dict(case.contract.get("softPreferences", {})),
        source_authority=ContractAuthority.SYSTEM_POLICY,
    )
    entities_by_key = {}
    for item in case.entities:
        entities_by_key[item["key"]] = EntityRef.create(
            tenant_id="tenant-1",
            campaign_id="campaign-1",
            entity_type=EntityType(item["type"]),
            label=item["key"],
            canonical_address=(
                "100 Target Rd" if item["relationship"] != "alternate"
                else "900 Replacement Rd"
            ),
            suite=item["key"] if item["type"] == "suite" else "",
            relationship=item["relationship"],
        )

    claims_by_key = {}
    for index, item in enumerate(case.claims):
        supersedes = item.get("supersedes")
        claims_by_key[item["key"]] = Claim.create(
            tenant_id="tenant-1",
            evidence_id=f"evidence-{case.case_id}-{index}",
            subject_entity_id=entities_by_key[item["subject"]].entity_id,
            predicate=ClaimPredicate(item["predicate"]),
            value=item["value"],
            evidence_text=f"fixture evidence {item['key']}",
            actor_role=ActorRole.BROKER,
            polarity=ClaimPolarity(item["polarity"]),
            modality=ClaimModality(item["modality"]),
            confidence=0.99,
            supersedes_claim_id=(
                claims_by_key[supersedes].claim_id if supersedes else None
            ),
            campaign_id="campaign-1",
            actor_email="broker@example.test",
            observed_at=f"2026-07-22T12:{index:02d}:00Z",
        )

    def remap(values):
        return {
            entities_by_key[key].entity_id: dict(value)
            for key, value in values.items()
        }

    request = PolicyEvaluationRequest.create(
        contract=contract,
        scope=ExecutionScope(
            tenant_id="tenant-1",
            client_id="client-1",
            campaign_id="campaign-1",
            thread_id="thread-1",
            sheet_id="sheet-1",
            row_anchor="100 Target Rd",
        ),
        entities=tuple(entities_by_key.values()),
        claims=tuple(claims_by_key.values()),
        snapshot_hash=f"snapshot-{case.case_id}",
        current_facts=remap(case.current_state["facts"]),
        current_conversation_states={
            entities_by_key[key].entity_id: value
            for key, value in case.current_state["conversationStates"].items()
        },
        current_followup_states={
            entities_by_key[key].entity_id: value
            for key, value in case.current_state["followupStates"].items()
        },
        authorized_recipients=("broker@example.test",),
    )
    return request, entities_by_key, claims_by_key


class PolicyReductionTests(unittest.TestCase):
    def test_scope_mismatch_fails_closed(self):
        case = load_policy_fixture_catalog(FIXTURE_PATH).cases[0]
        request, entities, claims = _build_case(case)
        wrong_entity = EntityRef.create(
            tenant_id="tenant-1",
            campaign_id="other-campaign",
            entity_type=EntityType.TARGET_PROPERTY,
            label="wrong",
            relationship="target",
        )

        with self.assertRaisesRegex(ValueError, "campaign"):
            PolicyEvaluationRequest.create(
                contract=request.contract,
                scope=request.scope,
                entities=(wrong_entity,),
                claims=(),
                snapshot_hash="snapshot-wrong",
            )

    def test_correction_supersedes_old_value(self):
        case = next(
            item
            for item in load_policy_fixture_catalog(FIXTURE_PATH).cases
            if item.case_id == "correction-supersedes-rent"
        )
        request, _, _ = _build_case(case)

        result = evaluate_policy(request)
        fact_updates = [
            action
            for action in result.results[0].action_plan.actions
            if action.action_type.value == "fact_update"
        ]

        self.assertEqual([13.5], [action.payload["value"] for action in fact_updates])

    def test_input_order_does_not_change_result(self):
        case = next(
            item
            for item in load_policy_fixture_catalog(FIXTURE_PATH).cases
            if item.case_id == "split-suite-mixed"
        )
        request, _, _ = _build_case(case)
        reversed_request = PolicyEvaluationRequest.create(
            contract=request.contract,
            scope=request.scope,
            entities=tuple(reversed(request.entities)),
            claims=tuple(reversed(request.claims)),
            snapshot_hash=request.snapshot_hash,
            current_facts=request.current_facts,
            current_conversation_states=request.current_conversation_states,
            current_followup_states=request.current_followup_states,
            authorized_recipients=request.authorized_recipients,
        )

        self.assertEqual(
            evaluate_policy(request).result_digest,
            evaluate_policy(reversed_request).result_digest,
        )


class PolicyDecisionTests(unittest.TestCase):
    def test_fixture_decisions_match_exact_oracle(self):
        catalog = load_policy_fixture_catalog(FIXTURE_PATH)
        for case in catalog.cases:
            with self.subTest(case_id=case.case_id):
                request, entities_by_key, _ = _build_case(case)
                key_by_id = {
                    entity.entity_id: key for key, entity in entities_by_key.items()
                }
                actual = {
                    key_by_id[item.decision.entity_id]: item
                    for item in evaluate_policy(request).results
                }
                self.assertEqual(
                    {item["subject"] for item in case.expected["results"]},
                    set(actual),
                )
                for expected in case.expected["results"]:
                    item = actual[expected["subject"]]
                    decision = item.decision
                    self.assertEqual(expected["marketState"], decision.market_state.value)
                    self.assertEqual(expected["fitState"], decision.fit_state.value)
                    self.assertEqual(
                        expected["completenessState"],
                        decision.completeness_state.value,
                    )
                    self.assertEqual(
                        expected["conversationState"],
                        decision.conversation_state.value,
                    )
                    self.assertEqual(
                        expected["approvalClass"],
                        item.approval_class.value,
                    )
                    self.assertEqual(
                        tuple(expected["reasonCodes"]),
                        decision.reason_codes,
                    )
                    self.assertEqual(
                        tuple(expected["missingFields"]),
                        decision.missing_fields,
                    )

    def test_conflict_is_review_not_unavailable(self):
        case = next(
            item
            for item in load_policy_fixture_catalog(FIXTURE_PATH).cases
            if item.case_id == "conflicting-availability"
        )
        request, _, _ = _build_case(case)
        item = evaluate_policy(request).results[0]

        self.assertEqual("unknown", item.decision.market_state.value)
        self.assertEqual(ConversationState.REVIEW, item.decision.conversation_state)
        self.assertIn("conflicting_active_claims", item.decision.reason_codes)


class PolicyActionTests(unittest.TestCase):
    def test_fixture_action_requirements_and_prohibitions(self):
        catalog = load_policy_fixture_catalog(FIXTURE_PATH)
        for case in catalog.cases:
            with self.subTest(case_id=case.case_id):
                request, entities_by_key, _ = _build_case(case)
                key_by_id = {
                    entity.entity_id: key for key, entity in entities_by_key.items()
                }
                actual = {
                    key_by_id[item.decision.entity_id]: item
                    for item in evaluate_policy(request).results
                }
                for expected in case.expected["results"]:
                    actions = actual[expected["subject"]].action_plan.actions
                    signatures = {
                        f"{action.action_type.value}:{action.approval_class.value}"
                        for action in actions
                    }
                    self.assertTrue(
                        set(expected["requiredActions"]) <= signatures,
                        (case.case_id, expected["subject"], signatures),
                    )
                    self.assertTrue(
                        set(expected["forbiddenActions"]).isdisjoint(signatures),
                        (case.case_id, expected["subject"], signatures),
                    )

    def test_terminal_intent_always_freezes_followups(self):
        for case in load_policy_fixture_catalog(FIXTURE_PATH).cases:
            request, _, _ = _build_case(case)
            for item in evaluate_policy(request).results:
                if item.decision.conversation_state is ConversationState.TERMINAL_INTENT:
                    self.assertIn(
                        "followup_freeze",
                        {action.action_type.value for action in item.action_plan.actions},
                        case.case_id,
                    )

    def test_no_policy_result_contains_an_outbound_draft(self):
        for case in load_policy_fixture_catalog(FIXTURE_PATH).cases:
            request, _, _ = _build_case(case)
            action_types = {
                action.action_type.value
                for item in evaluate_policy(request).results
                for action in item.action_plan.actions
            }
            self.assertNotIn("outbound_draft", action_types, case.case_id)

    def test_three_repeats_are_byte_stable(self):
        catalog = load_policy_fixture_catalog(FIXTURE_PATH)
        for case in catalog.cases:
            request, _, _ = _build_case(case)
            digests = {evaluate_policy(request).result_digest for _ in range(3)}
            self.assertEqual(1, len(digests), case.case_id)


if __name__ == "__main__":
    unittest.main()

