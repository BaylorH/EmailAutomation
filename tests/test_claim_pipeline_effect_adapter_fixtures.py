import json
import re
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, is_dataclass, replace
from pathlib import Path
from unittest.mock import patch

from email_automation.claim_pipeline import effect_adapter_fixtures as fixture_module
from email_automation.claim_pipeline.contracts import ActionType, Claim
from email_automation.claim_pipeline.effect_adapter import (
    DryRunReason,
    DryRunStatus,
    evaluate_effect_plan,
)
from email_automation.claim_pipeline.effect_adapter_fixtures import (
    EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION,
    EffectAdapterFixtureCase,
    EffectAdapterFixtureCatalog,
    EffectAdapterFixtureResult,
    EffectAdapterFixtureValidationError,
    load_effect_adapter_fixture_catalog,
    run_effect_adapter_fixture_case,
)


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "claim_pipeline_effect_adapter_cases.json"
)
REQUIRED_CASE_IDS = (
    "automatic-fact-matching",
    "automatic-fact-stale-prior",
    "whole-plan-stale-snapshot",
    "whole-plan-stale-contract",
    "already-committed-effect",
    "human-action-no-approval",
    "human-action-exact-approval",
    "approval-for-other-action",
    "approval-wrong-plan",
    "forbidden-plan",
    "unsupported-actions",
    "terminal-outbound-draft",
    "terminal-followup-freeze",
    "dependency-chain-eligible",
    "dependency-chain-blocked",
    "dependency-construction-rejected",
    "scope-and-provenance-rejected",
    "input-order-byte-stable",
)
REQUIRED_CASE_DEFINITIONS = (
    (
        "automatic-fact-matching",
        (("fact_update", "automatic", ()),),
        (),
        (("fact_update:1", "would_apply", "eligible_automatic_action"),),
    ),
    (
        "automatic-fact-stale-prior",
        (("fact_update", "automatic", ()),),
        ("stale_prior_state:1",),
        (("fact_update:1", "blocked", "prior_state_mismatch"),),
    ),
    (
        "whole-plan-stale-snapshot",
        (("fact_update", "automatic", ()),),
        ("stale_snapshot",),
        (("fact_update:1", "blocked", "stale_snapshot"),),
    ),
    (
        "whole-plan-stale-contract",
        (("fact_update", "automatic", ()),),
        ("stale_contract",),
        (("fact_update:1", "blocked", "stale_contract"),),
    ),
    (
        "already-committed-effect",
        (("fact_update", "automatic", ()),),
        ("committed:1",),
        (
            (
                "fact_update:1",
                "skipped",
                "idempotency_key_already_committed",
            ),
        ),
    ),
    (
        "human-action-no-approval",
        (("information_request", "human_required", ()),),
        (),
        (("information_request:1", "skipped", "approval_required"),),
    ),
    (
        "human-action-exact-approval",
        (("information_request", "human_required", ()),),
        ("approve:1",),
        (
            (
                "information_request:1",
                "would_apply",
                "eligible_human_approved_action",
            ),
        ),
    ),
    (
        "approval-for-other-action",
        (("information_request", "human_required", ()),),
        ("approval_other_action:1",),
        (("information_request:1", "skipped", "approval_required"),),
    ),
    (
        "approval-wrong-plan",
        (("information_request", "human_required", ()),),
        ("approval_wrong_plan:1",),
        (
            (
                "information_request:1",
                "blocked",
                "approval_scope_mismatch",
            ),
        ),
    ),
    (
        "forbidden-plan",
        (("fact_update", "forbidden", ()),),
        (),
        (("fact_update:1", "blocked", "plan_contract_violation"),),
    ),
    (
        "unsupported-actions",
        (
            ("note_append", "automatic", ()),
            ("row_move", "automatic", ()),
            ("notification", "automatic", ()),
            ("loi_request", "human_required", ()),
            ("outbound_draft", "human_required", ()),
        ),
        (),
        (
            ("note_append:1", "blocked", "unsupported_action_type"),
            ("row_move:2", "blocked", "unsupported_action_type"),
            ("notification:3", "blocked", "unsupported_action_type"),
            ("loi_request:4", "blocked", "unsupported_action_type"),
            ("outbound_draft:5", "blocked", "unsupported_action_type"),
        ),
    ),
    (
        "terminal-outbound-draft",
        (("outbound_draft", "human_required", ()),),
        ("terminal_decision",),
        (
            (
                "outbound_draft:1",
                "blocked",
                "terminal_outbound_suppressed",
            ),
        ),
    ),
    (
        "terminal-followup-freeze",
        (("followup_freeze", "automatic", ()),),
        ("terminal_decision",),
        (("followup_freeze:1", "would_apply", "eligible_automatic_action"),),
    ),
    (
        "dependency-chain-eligible",
        (
            ("fact_update", "automatic", ()),
            ("status_transition", "automatic", (1,)),
        ),
        (),
        (
            ("fact_update:1", "would_apply", "eligible_automatic_action"),
            (
                "status_transition:2",
                "would_apply",
                "eligible_automatic_action",
            ),
        ),
    ),
    (
        "dependency-chain-blocked",
        (
            ("fact_update", "automatic", ()),
            ("status_transition", "automatic", (1,)),
        ),
        ("stale_prior_state:1",),
        (
            ("fact_update:1", "blocked", "prior_state_mismatch"),
            ("status_transition:2", "blocked", "dependency_blocked"),
        ),
    ),
    (
        "dependency-construction-rejected",
        (
            ("fact_update", "automatic", (2,)),
            ("status_transition", "automatic", ()),
        ),
        (),
        (
            ("fact_update:1", "blocked", "plan_contract_violation"),
            ("status_transition:2", "blocked", "plan_contract_violation"),
        ),
    ),
    (
        "scope-and-provenance-rejected",
        (("fact_update", "automatic", ()),),
        ("scope_row_mismatch",),
        (("fact_update:1", "blocked", "plan_contract_violation"),),
    ),
    (
        "input-order-byte-stable",
        (
            ("fact_update", "automatic", ()),
            ("status_transition", "automatic", (1,)),
        ),
        ("reverse_request_collections",),
        (
            ("fact_update:1", "would_apply", "eligible_automatic_action"),
            (
                "status_transition:2",
                "would_apply",
                "eligible_automatic_action",
            ),
        ),
    ),
)
EMAIL_LIKE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


class EffectAdapterFixtureTests(unittest.TestCase):
    def _payload(self):
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def _assert_rejected(self, payload, pattern=None):
        self._assert_raw_rejected(json.dumps(payload), pattern)

    def _assert_raw_rejected(self, raw, pattern=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            if isinstance(raw, bytes):
                path.write_bytes(raw)
            else:
                path.write_text(raw, encoding="utf-8")
            context = self.assertRaises(EffectAdapterFixtureValidationError)
            with context:
                load_effect_adapter_fixture_catalog(path)
            if pattern is not None:
                self.assertRegex(str(context.exception), pattern)

    def _email_like_strings(self, value):
        if isinstance(value, str):
            return (value,) if EMAIL_LIKE.search(value) else ()
        if isinstance(value, Mapping):
            return tuple(
                match
                for key, item in value.items()
                for nested in (key, item)
                for match in self._email_like_strings(nested)
            )
        if isinstance(value, (list, tuple, set, frozenset)):
            return tuple(
                match
                for item in value
                for match in self._email_like_strings(item)
            )
        return ()

    def test_catalog_has_exact_schema_and_case_lattice(self):
        catalog = load_effect_adapter_fixture_catalog(FIXTURE_PATH)

        self.assertEqual(
            "claim-pipeline-effect-adapter-fixtures-v1",
            EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION,
        )
        self.assertEqual(
            EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION,
            catalog.schema_version,
        )
        self.assertEqual(18, len(catalog.cases))
        self.assertEqual(REQUIRED_CASE_IDS, tuple(case.case_id for case in catalog.cases))
        self.assertEqual(18, len({case.case_id for case in catalog.cases}))
        actual_definitions = tuple(
            (
                case.case_id,
                tuple(
                    (
                        action["type"],
                        action["approval"],
                        tuple(action.get("dependsOn", ())),
                    )
                    for action in case.actions
                ),
                case.mutations,
                tuple(
                    (
                        receipt["action"],
                        receipt["status"],
                        receipt["reason"],
                    )
                    for receipt in case.expected_receipts
                ),
            )
            for case in catalog.cases
        )
        self.assertEqual(REQUIRED_CASE_DEFINITIONS, actual_definitions)

    def test_all_statuses_and_reachable_dry_run_reasons_are_covered(self):
        catalog = load_effect_adapter_fixture_catalog(FIXTURE_PATH)
        statuses = {
            receipt["status"]
            for case in catalog.cases
            for receipt in case.expected_receipts
        }
        reasons = {
            receipt["reason"]
            for case in catalog.cases
            for receipt in case.expected_receipts
        }

        self.assertEqual({status.value for status in DryRunStatus}, statuses)
        self.assertEqual({reason.value for reason in DryRunReason}, reasons)

    def test_fixture_requests_and_results_contain_no_email_like_strings(self):
        raw = FIXTURE_PATH.read_text(encoding="utf-8")
        self.assertIsNone(EMAIL_LIKE.search(raw))

        catalog = load_effect_adapter_fixture_catalog(FIXTURE_PATH)
        for case in catalog.cases:
            with self.subTest(case=case.case_id):
                request = fixture_module._build_effect_adapter_request(case)
                self.assertEqual((), self._email_like_strings(request.to_dict()))

        results = tuple(run_effect_adapter_fixture_case(case) for case in catalog.cases)
        serialized = json.dumps(
            [
                {
                    "caseId": result.case_id,
                    "passed": result.passed,
                    "receiptId": result.receipt_id,
                    "receipts": [dict(receipt) for receipt in result.receipts],
                }
                for result in results
            ],
            sort_keys=True,
        )
        self.assertIsNone(EMAIL_LIKE.search(serialized))

    def test_recursive_privacy_scan_detects_nested_request_claim_value(self):
        catalog = load_effect_adapter_fixture_catalog(FIXTURE_PATH)
        case = catalog.cases[0]
        request = fixture_module._build_effect_adapter_request(case)
        source = request.claims[0]
        privacy_probe = "privacy-probe@" + "example.invalid"
        tainted_claim = Claim.create(
            tenant_id=source.tenant_id,
            evidence_id=source.evidence_id,
            subject_entity_id=source.subject_entity_id,
            predicate=source.predicate,
            value={"nested": {"contact": privacy_probe}},
            evidence_text=source.evidence_text,
            actor_role=source.actor_role,
            polarity=source.polarity,
            modality=source.modality,
            confidence=source.confidence,
            unit=source.unit,
            effective_at=source.effective_at,
            supersedes_claim_id=source.supersedes_claim_id,
            campaign_id=source.campaign_id,
            actor_email=source.actor_email,
            observed_at=source.observed_at,
        )
        tainted_request = replace(
            request,
            claims=(tainted_claim, *request.claims[1:]),
        )

        with patch.object(
            fixture_module,
            "_build_effect_adapter_request",
            return_value=tainted_request,
        ):
            built = fixture_module._build_effect_adapter_request(case)

        self.assertEqual(
            (privacy_probe,),
            self._email_like_strings(built.to_dict()),
        )

    def test_every_case_matches_the_complete_ordered_receipt_oracle(self):
        catalog = load_effect_adapter_fixture_catalog(FIXTURE_PATH)

        for case in catalog.cases:
            with self.subTest(case=case.case_id):
                result = run_effect_adapter_fixture_case(case)
                expected = tuple(
                    (
                        receipt["action"],
                        receipt["status"],
                        receipt["reason"],
                    )
                    for receipt in case.expected_receipts
                )
                actual = tuple(
                    (
                        receipt["action"],
                        receipt["status"],
                        receipt["reason"],
                    )
                    for receipt in result.receipts
                )
                self.assertTrue(result.passed)
                self.assertEqual(expected, actual)
                self.assertEqual(len(case.actions), len(result.receipts))

    def test_input_order_case_runs_repeated_and_reversed_collection_proof(self):
        catalog = load_effect_adapter_fixture_catalog(FIXTURE_PATH)
        case = next(
            item for item in catalog.cases if item.case_id == "input-order-byte-stable"
        )

        with patch(
            "email_automation.claim_pipeline.effect_adapter_fixtures.evaluate_effect_plan",
            wraps=evaluate_effect_plan,
        ) as evaluator:
            result = run_effect_adapter_fixture_case(case)

        self.assertTrue(result.passed)
        self.assertEqual(3, evaluator.call_count)
        first, repeated, reversed_request = (
            call.args[0] for call in evaluator.call_args_list
        )
        self.assertIs(first, repeated)
        for attribute in (
            "entities",
            "claims",
            "current_states",
            "approval_grants",
            "committed_idempotency_keys",
            "authorized_recipients",
        ):
            original = getattr(first, attribute)
            reversed_value = getattr(reversed_request, attribute)
            self.assertGreaterEqual(len(original), 2)
            self.assertEqual(original, tuple(reversed(reversed_value)))
            self.assertNotEqual(original, reversed_value)

        commits = tuple(
            evaluate_effect_plan(request)
            for request in (first, repeated, reversed_request)
        )
        self.assertEqual(
            (commits[0].receipt_id,) * 3,
            tuple(commit.receipt_id for commit in commits),
        )
        self.assertEqual(
            json.dumps(commits[0].to_dict(), sort_keys=True),
            json.dumps(commits[2].to_dict(), sort_keys=True),
        )
        self.assertEqual(commits[0].receipt_id, result.receipt_id)

    def test_fixture_contract_types_are_frozen_dataclasses(self):
        catalog = load_effect_adapter_fixture_catalog(FIXTURE_PATH)
        result = run_effect_adapter_fixture_case(catalog.cases[0])
        values = (
            catalog.cases[0],
            catalog,
            result,
            EffectAdapterFixtureValidationError("fixture-error"),
        )

        for value in values:
            with self.subTest(value=type(value).__name__):
                self.assertTrue(is_dataclass(value))
                field_name = next(iter(value.__dataclass_fields__))
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, field_name, getattr(value, field_name))

        self.assertIsInstance(catalog.cases[0], EffectAdapterFixtureCase)
        self.assertIsInstance(catalog, EffectAdapterFixtureCatalog)
        self.assertIsInstance(result, EffectAdapterFixtureResult)

    def test_wrong_schema_and_duplicate_case_ids_are_rejected(self):
        wrong_schema = self._payload()
        wrong_schema["schemaVersion"] = "claim-pipeline-effect-adapter-fixtures-v0"
        self._assert_rejected(wrong_schema, "schemaVersion")

        duplicate = self._payload()
        duplicate["cases"].append(dict(duplicate["cases"][0]))
        self._assert_rejected(duplicate, "duplicate caseId")

    def test_duplicate_json_object_keys_are_rejected_at_every_depth(self):
        raw_catalog = FIXTURE_PATH.read_text(encoding="utf-8")
        schema_entry = (
            f'"schemaVersion": "{EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION}"'
        )
        duplicate_root = raw_catalog.replace(
            schema_entry,
            f"{schema_entry},\n  {schema_entry}",
            1,
        )
        duplicate_action = (
            '{"schemaVersion":"'
            + EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION
            + '","cases":[{"caseId":"duplicate-action-key",'
            '"actions":[{"type":"fact_update","type":"fact_update",'
            '"approval":"automatic"}],"mutations":[],"expectedReceipts":'
            '[{"action":"fact_update:1","status":"would_apply",'
            '"reason":"eligible_automatic_action"}]}]}'
        )

        for label, raw in (
            ("root", duplicate_root),
            ("action", duplicate_action),
        ):
            with self.subTest(level=label):
                self._assert_raw_rejected(raw, "duplicate JSON object key")

    def test_whitespace_padded_tokens_are_rejected(self):
        mutations = (
            (
                "action",
                lambda payload: payload["cases"][0]["actions"][0].__setitem__(
                    "type", " fact_update"
                ),
            ),
            (
                "mutation",
                lambda payload: payload["cases"][0].__setitem__(
                    "mutations", ["stale_snapshot "]
                ),
            ),
            (
                "status",
                lambda payload: payload["cases"][0]["expectedReceipts"][
                    0
                ].__setitem__("status", " would_apply"),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(token=label):
                payload = self._payload()
                mutate(payload)
                self._assert_rejected(payload, "surrounding whitespace")

    def test_file_decode_and_json_parse_failures_are_normalized(self):
        oversized_integer = "1" * 10000
        malformed_inputs = (
            ("invalid-utf8", b'{"schemaVersion":"\xff"}', "cannot be read"),
            ("malformed-json", '{"schemaVersion":', "cannot be read"),
            (
                "oversized-integer",
                '{"schemaVersion":"'
                + EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION
                + '","cases":'
                + oversized_integer
                + "}",
                "cannot be read",
            ),
        )
        for label, raw, pattern in malformed_inputs:
            with self.subTest(input=label):
                self._assert_raw_rejected(raw, pattern)

    def test_nonstandard_json_constants_are_rejected_during_parsing(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                raw = (
                    '{"schemaVersion":"'
                    + EFFECT_ADAPTER_FIXTURE_SCHEMA_VERSION
                    + '","cases":'
                    + constant
                    + "}"
                )
                self._assert_raw_rejected(raw, "nonstandard JSON constant")

    def test_unknown_action_approval_and_mutation_tokens_are_rejected(self):
        mutations = (
            ("actions", "type", "send_message", "action type"),
            ("actions", "approval", "operator_optional", "approval"),
        )
        for _, key, token, pattern in mutations:
            with self.subTest(token=token):
                payload = self._payload()
                payload["cases"][0]["actions"][0][key] = token
                self._assert_rejected(payload, pattern)

        payload = self._payload()
        payload["cases"][0]["mutations"] = ["connect_service"]
        self._assert_rejected(payload, "mutation")

    def test_valid_production_actions_outside_fixture_vocabulary_are_rejected(self):
        fixture_action_types = {
            action[0]
            for _, actions, _, _ in REQUIRED_CASE_DEFINITIONS
            for action in actions
        }
        excluded = tuple(
            action_type
            for action_type in ActionType
            if action_type.value not in fixture_action_types
        )
        self.assertEqual(
            {
                ActionType.ALTERNATE_PROPERTY_PROPOSAL,
                ActionType.CALL_REQUEST,
                ActionType.RECIPIENT_CHANGE,
                ActionType.REVIEW_ITEM,
                ActionType.TOUR_REQUEST,
            },
            set(excluded),
        )

        for action_type in excluded:
            with self.subTest(action_type=action_type.value):
                payload = self._payload()
                payload["cases"][0]["actions"][0]["type"] = action_type.value
                payload["cases"][0]["expectedReceipts"][0][
                    "action"
                ] = f"{action_type.value}:1"
                self._assert_rejected(payload, "fixture action type")

    def test_invalid_dependencies_are_rejected(self):
        invalid_values = ([0], [2], [3], ["1"], [1, 1])
        for depends_on in invalid_values:
            with self.subTest(depends_on=depends_on):
                payload = self._payload()
                payload["cases"][13]["actions"][1]["dependsOn"] = depends_on
                self._assert_rejected(payload, "dependsOn")

    def test_malformed_expected_receipts_are_rejected(self):
        changes = (
            ("status", "applied", "status"),
            ("reason", "sent_message", "reason"),
            ("action", "fact_update", "action"),
            ("action", "notification:1", "does not match"),
            ("action", "fact_update:2", "sequence"),
        )
        for key, value, pattern in changes:
            with self.subTest(key=key, value=value):
                payload = self._payload()
                payload["cases"][0]["expectedReceipts"][0][key] = value
                self._assert_rejected(payload, pattern)

    def test_unknown_fields_are_rejected_at_every_schema_level(self):
        mutations = (
            lambda value: value.__setitem__("catalogId", "opaque"),
            lambda value: value["cases"][0].__setitem__("description", "opaque"),
            lambda value: value["cases"][0]["actions"][0].__setitem__(
                "payload", {}
            ),
            lambda value: value["cases"][0]["expectedReceipts"][0].__setitem__(
                "detail", "opaque"
            ),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                payload = self._payload()
                mutate(payload)
                self._assert_rejected(payload, "unknown")


if __name__ == "__main__":
    unittest.main()
