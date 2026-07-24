import json
import hashlib
import unittest
from dataclasses import FrozenInstanceError, replace

from email_automation.claim_pipeline.contracts import (
    ActionPlan,
    ActionType,
    ActorRole,
    ApprovalClass,
    Claim,
    ClaimModality,
    ClaimPolarity,
    ClaimPredicate,
    PlannedAction,
)
from email_automation.claim_pipeline.effect_adapter import DryRunReason, DryRunStatus
from email_automation.claim_pipeline.effect_adapter import (
    DryRunCommitReceipt,
    DryRunEffectReceipt,
)
from email_automation.claim_pipeline.disabled_evidence import (
    EvidenceActionRow,
    EvidenceClaimRow,
    DisabledEvidenceEnvelope,
    EvidenceDisposition,
    EvidenceContentHashes,
    EvidenceDestinationAttestation,
    EvidenceProvenance,
    EvidenceSummary,
    EvidenceTimestamps,
    ZeroEffectAttestation,
    FixtureTrustAnchor,
    canonical_json,
    classify_duplicate_envelope,
    derive_run_id,
    project_disabled_evidence,
    serialize_disabled_evidence,
    verify_disabled_evidence_envelope,
)


HASH = "a" * 64
COMMIT = "b" * 40


def timestamps():
    return EvidenceTimestamps(
        evaluation_started_at="2026-07-24T12:00:00Z",
        evaluation_completed_at="2026-07-24T12:00:01Z",
        captured_at="2026-07-24T12:00:02Z",
    )


def content_hashes():
    return EvidenceContentHashes(
        source_sha256=HASH,
        fixture_sha256="b" * 64,
        projection_sha256="c" * 64,
        receipt_payload_sha256="d" * 64,
        payload_sha256="e" * 64,
        envelope_sha256="f" * 64,
    )


def zero_effect_attestation():
    return ZeroEffectAttestation(
        verified_source_sha256=(
            "b391407cd5bbc673874eb80bbf488ba4cfdb04b285daaf45cd34f0bf6052d634"
        ),
        verified_report_sha256="5" * 64,
        verified_result_digest="6" * 64,
        test_manifest_sha256="3" * 64,
        isolation_tests_passed=19,
        verifier_id="fixture-verifier",
        verifier_version="fixture-v1",
        verification_run_id="verification-run-1",
        signature="fixture-signature",
    )


def destination_attestation():
    return EvidenceDestinationAttestation(
        environment="local_fixture",
        project_or_store="store_fixture",
        namespace="namespace_fixture",
        deployment_identity_sha256="4" * 64,
    )


def fixture_trust_anchor():
    return FixtureTrustAnchor(
        verifier_id="fixture-verifier",
        verifier_version="fixture-v1",
        signature="fixture-signature",
    )


def tainted_claim():
    return Claim.create(
        tenant_id="tenant-1",
        evidence_id="evidence-raw-customer-content",
        subject_entity_id="entity-1",
        predicate=ClaimPredicate.AVAILABILITY,
        value={"sheetValue": "123 Industrial Ave", "recipient": "pat@example.invalid"},
        evidence_text="Pat says 123 Industrial Ave is available.",
        actor_role=ActorRole.BROKER,
        polarity=ClaimPolarity.POSITIVE,
        modality=ClaimModality.ASSERTED,
        confidence=0.95,
        campaign_id="campaign-raw-name",
        actor_email="pat@example.invalid",
        observed_at="2026-07-24T12:00:00Z",
    )


def tainted_plan_and_receipt():
    claim = tainted_claim()
    action = PlannedAction.create(
        tenant_id="tenant-1",
        client_id="client-1",
        campaign_id="campaign-raw-name",
        thread_id="thread-raw",
        sheet_id="sheet-raw",
        row_anchor="row-raw",
        decision_id="decision-1",
        contract_id="contract-1",
        action_type=ActionType.FACT_UPDATE,
        approval_class=ApprovalClass.AUTOMATIC,
        target_entity_id="entity-1",
        contract_version=1,
        snapshot_hash="snapshot-1",
        source_claim_ids=(claim.claim_id,),
        operation_key="availability",
        expected_prior_state={"value": "raw prior Sheet value"},
        dependencies=(),
        sequence=1,
        recipient="pat@example.invalid",
        payload={"newValue": "123 Industrial Ave", "graphId": "graph-raw-id"},
        reason="raw exception-like reason",
    )
    plan = ActionPlan.create(
        tenant_id="tenant-1",
        client_id="client-1",
        campaign_id="campaign-raw-name",
        decision_id="decision-1",
        contract_id="contract-1",
        contract_version=1,
        snapshot_hash="snapshot-1",
        actions=(action,),
    )
    effect = DryRunEffectReceipt.create(
        plan_id=plan.plan_id,
        action_id=action.action_id,
        idempotency_key=action.idempotency_key,
        action_type=action.action_type.value,
        sequence=action.sequence,
        status=DryRunStatus.WOULD_APPLY,
        reason=DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
        dependency_receipt_ids=(),
    )
    receipt = DryRunCommitReceipt.create(
        tenant_id=plan.tenant_id,
        plan_id=plan.plan_id,
        decision_id=plan.decision_id,
        contract_id=plan.contract_id,
        contract_version=plan.contract_version,
        snapshot_hash=plan.snapshot_hash,
        effects=(effect,),
    )
    return claim, plan, receipt


def provenance():
    return EvidenceProvenance(
        code_revision=COMMIT,
        evidence_commit="c" * 40,
        report_sha256="5" * 64,
        result_digest="6" * 64,
        fixture_schema="claim-pipeline-effect-adapter-fixtures-v1",
        source_marker="fixture",
        fixture_ref="fixture_case_1",
    )


class DisabledEvidenceSchemaTests(unittest.TestCase):
    def test_disposition_taxonomy_has_exact_declared_values(self):
        self.assertEqual(
            (
                "proposed",
                "blocked_by_policy",
                "blocked_by_disabled_adapter",
                "invalid_input",
                "unknown_taxonomy",
            ),
            tuple(item.value for item in EvidenceDisposition),
        )

    def test_envelope_rejects_adapter_mode_other_than_disabled(self):
        provenance = EvidenceProvenance(
            code_revision=COMMIT,
            evidence_commit="c" * 40,
            report_sha256="5" * 64,
            result_digest="6" * 64,
            fixture_schema="claim-pipeline-effect-adapter-fixtures-v1",
            source_marker="fixture",
            fixture_ref="fixture_case_1",
        )

        with self.assertRaises(ValueError):
            DisabledEvidenceEnvelope(
                run_id="run_" + "7" * 64,
                taxonomy_version="sitesift-evidence-disposition-v1",
                provenance=provenance,
                adapter_mode="enabled",
                environment_marker="local_fixture",
                timestamps=timestamps(),
                content_hashes=content_hashes(),
                zero_effect_attestation=zero_effect_attestation(),
                destination_attestation=destination_attestation(),
                summary=EvidenceSummary(claim_count=0, action_count=0, warning_count=0),
                rows=(),
            )

    def test_rows_preserve_policy_status_separately_from_execution_status(self):
        claim = EvidenceClaimRow.create(
            sequence=1,
            claim_id="claim-alpha",
            source_category="fixture",
        )
        action = EvidenceActionRow.create(
            sequence=2,
            action_id="action-alpha",
            action_type="fact_update",
            source_claim_ids=("claim-alpha",),
            dependency_action_ids=(),
            policy_status=DryRunStatus.WOULD_APPLY,
            policy_reason=DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
            source_category="fixture",
        )

        self.assertEqual(EvidenceDisposition.PROPOSED, claim.disposition)
        self.assertEqual("not_applicable_claim", claim.execution_status)
        self.assertEqual("would_apply", action.policy_status)
        self.assertEqual("not_attempted_adapter_disabled", action.execution_status)
        self.assertEqual(
            EvidenceDisposition.BLOCKED_BY_DISABLED_ADAPTER,
            action.disposition,
        )
        self.assertTrue(action.row_id.startswith("row_"))
        self.assertNotIn("action-alpha", action.row_id)
        with self.assertRaises(FrozenInstanceError):
            action.execution_status = "changed"
        with self.assertRaises(ValueError):
            EvidenceActionRow(
                row_id="row_invalid",
                sequence=3,
                action_ref="action_ref",
                action_type="fact_update",
                claim_refs=("claim_ref",),
                dependency_refs=(),
                policy_status="blocked",
                policy_reason="unsupported_action_type",
                execution_status="not_attempted_policy_gate",
                disposition=EvidenceDisposition.INVALID_INPUT,
                source_category="fixture",
            )
        with self.assertRaises(ValueError):
            EvidenceActionRow.create(
                sequence=4,
                action_id="action-invalid-combo",
                action_type="fact_update",
                source_claim_ids=("claim-alpha",),
                dependency_action_ids=(),
                policy_status=DryRunStatus.WOULD_APPLY,
                policy_reason=DryRunReason.UNSUPPORTED_ACTION_TYPE,
                source_category="fixture",
            )

    def test_envelope_recomputes_summary_and_enforces_row_limit(self):
        claim_row = EvidenceClaimRow.create(
            sequence=1,
            claim_id="claim-summary",
            source_category="fixture",
        )
        with self.assertRaises(ValueError):
            DisabledEvidenceEnvelope(
                run_id="run_" + "7" * 64,
                taxonomy_version="sitesift-evidence-disposition-v1",
                provenance=provenance(),
                adapter_mode="disabled",
                environment_marker="local_fixture",
                timestamps=timestamps(),
                content_hashes=content_hashes(),
                zero_effect_attestation=zero_effect_attestation(),
                destination_attestation=destination_attestation(),
                summary=EvidenceSummary(claim_count=0, action_count=0, warning_count=0),
                rows=(claim_row,),
            )

        too_many_rows = tuple(
            EvidenceClaimRow.create(
                sequence=index,
                claim_id=f"claim-{index}",
                source_category="fixture",
            )
            for index in range(1, 402)
        )
        with self.assertRaises(ValueError):
            DisabledEvidenceEnvelope(
                run_id="run_" + "8" * 64,
                taxonomy_version="sitesift-evidence-disposition-v1",
                provenance=provenance(),
                adapter_mode="disabled",
                environment_marker="local_fixture",
                timestamps=timestamps(),
                content_hashes=content_hashes(),
                zero_effect_attestation=zero_effect_attestation(),
                destination_attestation=destination_attestation(),
                summary=EvidenceSummary(
                    claim_count=401,
                    action_count=0,
                    warning_count=0,
                ),
                rows=too_many_rows,
            )


class DisabledEvidenceProjectorTests(unittest.TestCase):
    def test_projector_emits_only_sanitized_opaque_references(self):
        claim, plan, receipt = tainted_plan_and_receipt()

        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )
        serialized = json.dumps(projection.to_dict(), sort_keys=True)

        self.assertEqual(1, projection.summary.claim_count)
        self.assertEqual(1, projection.summary.action_count)
        self.assertEqual(2, len(projection.rows))
        self.assertEqual("would_apply", projection.rows[1].policy_status)
        self.assertEqual(
            "not_attempted_adapter_disabled",
            projection.rows[1].execution_status,
        )
        for forbidden in (
            "123 Industrial",
            "pat@example",
            "campaign-raw-name",
            "sheet-raw",
            "graph-raw-id",
            "raw exception-like reason",
            "raw prior Sheet value",
        ):
            self.assertNotIn(forbidden, serialized)


class DisabledEvidenceSerializerTests(unittest.TestCase):
    def test_serializer_is_canonical_and_derives_run_id(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )

        envelope = serialize_disabled_evidence(
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=zero_effect_attestation(),
            destination_attestation=destination_attestation(),
        )
        repeated = serialize_disabled_evidence(
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=zero_effect_attestation(),
            destination_attestation=destination_attestation(),
        )

        expected_run_id = derive_run_id(
            receipt_id=receipt.receipt_id,
            projection_sha256=envelope.content_hashes.projection_sha256,
            fixture_sha256=envelope.content_hashes.fixture_sha256,
            code_revision=envelope.provenance.code_revision,
            result_digest=envelope.provenance.result_digest,
        )
        self.assertEqual(expected_run_id, envelope.run_id)
        self.assertRegex(envelope.run_id, r"^run_[0-9a-f]{64}$")
        self.assertEqual(
            hashlib.sha256(canonical_json(projection.to_dict())).hexdigest(),
            envelope.content_hashes.projection_sha256,
        )
        self.assertEqual(
            hashlib.sha256(canonical_json(receipt.to_dict())).hexdigest(),
            envelope.content_hashes.receipt_payload_sha256,
        )
        self.assertEqual(envelope, repeated)
        self.assertEqual(
            canonical_json(envelope.to_dict()),
            canonical_json(repeated.to_dict()),
        )
        self.assertEqual(
            "sitesift-disabled-evidence-v1",
            envelope.schema_version,
        )
        with self.assertRaises(TypeError):
            serialize_disabled_evidence(
                receipt=receipt,
                projection=projection,
                provenance=provenance(),
                timestamps=timestamps(),
                zero_effect_attestation=zero_effect_attestation(),
                destination_attestation=destination_attestation(),
                run_id="run_caller_selected",
            )


class DisabledEvidenceVerifierTests(unittest.TestCase):
    def test_verifier_requires_independent_zero_effect_attestation(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )
        envelope = serialize_disabled_evidence(
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=zero_effect_attestation(),
            destination_attestation=destination_attestation(),
        )

        verified = verify_disabled_evidence_envelope(
            envelope,
            trust_anchor=fixture_trust_anchor(),
        )
        self.assertTrue(verified.verified)
        self.assertTrue(verified.include_in_normal_reads)

        with self.assertRaises(TypeError):
            verify_disabled_evidence_envelope(
                receipt,
                trust_anchor=fixture_trust_anchor(),
            )

        missing = replace(envelope, zero_effect_attestation=None)
        missing_result = verify_disabled_evidence_envelope(
            missing,
            trust_anchor=fixture_trust_anchor(),
        )
        self.assertFalse(missing_result.verified)
        self.assertFalse(missing_result.include_in_normal_reads)
        self.assertEqual("missing_zero_effect_attestation", missing_result.failure_code)

        forged = replace(
            envelope,
            zero_effect_attestation=replace(
                envelope.zero_effect_attestation,
                signature="forged-signature",
            ),
        )
        forged_result = verify_disabled_evidence_envelope(
            forged,
            trust_anchor=fixture_trust_anchor(),
        )
        self.assertFalse(forged_result.verified)
        self.assertFalse(forged_result.include_in_normal_reads)
        self.assertEqual("invalid_zero_effect_attestation", forged_result.failure_code)

        unsupported_taxonomy = replace(
            envelope,
            taxonomy_version="sitesift-evidence-disposition-v2",
        )
        taxonomy_result = verify_disabled_evidence_envelope(
            unsupported_taxonomy,
            trust_anchor=fixture_trust_anchor(),
        )
        self.assertFalse(taxonomy_result.verified)
        self.assertFalse(taxonomy_result.include_in_normal_reads)
        self.assertEqual("unsupported_taxonomy", taxonomy_result.failure_code)
        self.assertEqual(
            EvidenceDisposition.UNKNOWN_TAXONOMY,
            taxonomy_result.warning_disposition,
        )


class DisabledEvidenceDuplicateTests(unittest.TestCase):
    def test_same_run_id_different_envelope_hash_conflicts_without_overwrite(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )
        original = serialize_disabled_evidence(
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=zero_effect_attestation(),
            destination_attestation=destination_attestation(),
        )
        changed = replace(
            original,
            content_hashes=replace(
                original.content_hashes,
                envelope_sha256="0" * 64,
            ),
        )

        same = classify_duplicate_envelope(original, original)
        conflict = classify_duplicate_envelope(original, changed)

        self.assertEqual("same_hash_duplicate", same.outcome)
        self.assertFalse(same.should_write)
        self.assertEqual("conflict", conflict.outcome)
        self.assertFalse(conflict.should_write)
        self.assertTrue(conflict.preserve_original)


if __name__ == "__main__":
    unittest.main()
