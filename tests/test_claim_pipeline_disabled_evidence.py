import json
import hashlib
import unittest
from dataclasses import FrozenInstanceError, dataclass, replace
from types import SimpleNamespace

from email_automation.claim_pipeline import disabled_evidence as disabled_evidence_module
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
    EvidenceProjection,
    EvidenceProvenance,
    EvidenceSummary,
    EvidenceTimestamps,
    ZeroEffectAttestation,
    FixtureTrustAnchor,
    bind_fixture_evidence_envelope,
    canonical_json,
    classify_duplicate_envelope,
    derive_run_id,
    project_disabled_evidence,
    serialize_disabled_evidence,
    verify_disabled_evidence_envelope,
)


HASH = "a" * 64
COMMIT = "b" * 40
BASELINE_CODE_REVISION = "5a09a67729fb3054298a92cebf40937056c48647"
BASELINE_EVIDENCE_COMMIT = "df8425269c1ce3ab9bc4611705706d78c39dff02"
BASELINE_REPORT_SHA256 = (
    "33103b700cebe55133d3d97a6dba8743a3961cd49040e88e8807c8d5bbc9c7b2"
)
BASELINE_RESULT_DIGEST = (
    "450124af49e8c7827ee14ca99cdc13056865103a771a7028b20fb9b1ada63d7e"
)


def fixture_signature(attestation_fields):
    return hashlib.sha256(
        b"sitesift-zero-effect-attestation-fixture-v1\0"
        + canonical_json(attestation_fields)
    ).hexdigest()


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
    fields = {
        "attestation_schema": "sitesift-zero-effect-attestation-v1",
        "verified_source_sha256": (
            "b391407cd5bbc673874eb80bbf488ba4cfdb04b285daaf45cd34f0bf6052d634"
        ),
        "verified_report_sha256": BASELINE_REPORT_SHA256,
        "verified_result_digest": BASELINE_RESULT_DIGEST,
        "test_manifest_sha256": "3" * 64,
        "isolation_tests_passed": 19,
        "verifier_id": "fixture-verifier",
        "verifier_version": "fixture-v1",
        "verification_run_id": "verification-run-1",
    }
    return ZeroEffectAttestation(
        **fields,
        signature=fixture_signature(fields),
    )


def envelope_with_rows(rows, *, envelope_provenance=None):
    return DisabledEvidenceEnvelope(
        run_id="run_" + "7" * 64,
        taxonomy_version="sitesift-evidence-disposition-v1",
        provenance=envelope_provenance or provenance(),
        adapter_mode="disabled",
        environment_marker="local_fixture",
        timestamps=timestamps(),
        content_hashes=content_hashes(),
        zero_effect_attestation=zero_effect_attestation(),
        destination_attestation=destination_attestation(),
        summary=EvidenceSummary(
            claim_count=sum(isinstance(row, EvidenceClaimRow) for row in rows),
            action_count=sum(isinstance(row, EvidenceActionRow) for row in rows),
            warning_count=0,
        ),
        rows=rows,
    )


def action_row(*, sequence=1, claim_refs=(), dependency_refs=()):
    return EvidenceActionRow(
        row_id="row_" + f"{sequence:064x}",
        sequence=sequence,
        action_ref="action_ref_" + f"{sequence:064x}",
        action_type="fact_update",
        claim_refs=claim_refs,
        dependency_refs=dependency_refs,
        policy_status="would_apply",
        policy_reason="eligible_automatic_action",
        execution_status="not_attempted_adapter_disabled",
        disposition=EvidenceDisposition.BLOCKED_BY_DISABLED_ADAPTER,
        source_category="fixture",
    )


def claim_row(*, sequence=1):
    return EvidenceClaimRow(
        row_id="row_" + f"{sequence:064x}",
        sequence=sequence,
        claim_ref="claim_ref_" + f"{sequence:064x}",
        execution_status="not_applicable_claim",
        disposition=EvidenceDisposition.PROPOSED,
        source_category="fixture",
    )


def reserialized_envelope(
    *,
    plan,
    receipt,
    projection,
    attestation,
    envelope_provenance=None,
):
    payload = serialize_disabled_evidence(
        plan=plan,
        receipt=receipt,
        projection=projection,
        provenance=envelope_provenance or provenance(),
        timestamps=timestamps(),
        zero_effect_attestation=attestation,
    )
    return bind_fixture_evidence_envelope(
        payload,
        plan=plan,
        receipt=receipt,
        trust_anchor=fixture_trust_anchor(),
    )


def invalid_timestamp_cases():
    return (
        {
            "evaluation_started_at": "2026-07-24 12:00:00Z",
            "evaluation_completed_at": "2026-07-24T12:00:01Z",
            "captured_at": "2026-07-24T12:00:02Z",
        },
        {
            "evaluation_started_at": "2026-07-24T12:00:00",
            "evaluation_completed_at": "2026-07-24T12:00:01",
            "captured_at": "2026-07-24T12:00:02",
        },
        {
            "evaluation_started_at": "2026-07-24T12:00:02Z",
            "evaluation_completed_at": "2026-07-24T12:00:01Z",
            "captured_at": "2026-07-24T12:00:03Z",
        },
        {
            "evaluation_started_at": "2026-07-24T12:00:00Z",
            "evaluation_completed_at": "2026-07-24T12:00:03Z",
            "captured_at": "2026-07-24T12:00:02Z",
        },
    )


def invalid_provenance_cases():
    return (
        ("code_revision", "not-a-commit"),
        ("evidence_commit", "C" * 40),
        ("report_sha256", "5" * 63),
        ("result_digest", "G" * 64),
        ("fixture_schema", "unknown-fixture-schema"),
        ("source_marker", "live"),
        ("fixture_ref", "pat@example.invalid"),
        ("fixture_ref", "x" * 129),
    )


def invalid_destination_cases():
    return (
        ("environment", "production"),
        ("project_or_store", "email-automation-cache"),
        ("project_or_store", "EMAIL-AUTOMATION-CACHE"),
        ("project_or_store", "email-automation-cache-prod"),
        ("project_or_store", "prod-evidence-bucket"),
        ("project_or_store", "pat@example.invalid"),
        ("namespace", "customer namespace"),
        ("deployment_identity_sha256", "4" * 63),
    )


def valid_status_reasons():
    return {
        DryRunStatus.WOULD_APPLY: {
            DryRunReason.ELIGIBLE_AUTOMATIC_ACTION,
            DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION,
        },
        DryRunStatus.SKIPPED: {
            DryRunReason.APPROVAL_REQUIRED,
            DryRunReason.IDEMPOTENCY_KEY_ALREADY_COMMITTED,
        },
        DryRunStatus.BLOCKED: {
            DryRunReason.APPROVAL_SCOPE_MISMATCH,
            DryRunReason.UNSUPPORTED_ACTION_TYPE,
            DryRunReason.STALE_SNAPSHOT,
            DryRunReason.STALE_CONTRACT,
            DryRunReason.PRIOR_STATE_MISMATCH,
            DryRunReason.DEPENDENCY_BLOCKED,
            DryRunReason.TERMINAL_OUTBOUND_SUPPRESSED,
            DryRunReason.PLAN_CONTRACT_VIOLATION,
        },
    }


def projection_and_receipt():
    claim, plan, receipt = tainted_plan_and_receipt()
    projection = project_disabled_evidence(
        plan=plan,
        claims=(claim,),
        receipt=receipt,
    )
    return claim, plan, receipt, projection


def oversized_structured_rows():
    claims = tuple(claim_row(sequence=index) for index in range(1, 201))
    refs = tuple(row.claim_ref for row in claims)
    actions = tuple(
        action_row(sequence=index, claim_refs=refs)
        for index in range(201, 401)
    )
    return claims + actions


def exact_size_boundary_rows():
    claims = tuple(claim_row(sequence=index) for index in range(1, 51))
    refs = tuple(row.claim_ref for row in claims)
    actions = tuple(
        action_row(
            sequence=50 + index,
            claim_refs=refs[:4 if index <= 97 else 3],
        )
        for index in range(1, 351)
    )
    return claims + actions


def empty_envelope():
    return DisabledEvidenceEnvelope(
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
        rows=(),
    )


@dataclass(frozen=True)
class TaintedClaimRow(EvidenceClaimRow):
    raw_customer_content: str = "pat@example.invalid"


def legacy_zero_effect_attestation():
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
    attestation = zero_effect_attestation()
    return FixtureTrustAnchor(
        verifier_id="fixture-verifier",
        verifier_version="fixture-v1",
        signature=attestation.signature,
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
        code_revision=BASELINE_CODE_REVISION,
        evidence_commit=BASELINE_EVIDENCE_COMMIT,
        report_sha256=BASELINE_REPORT_SHA256,
        result_digest=BASELINE_RESULT_DIGEST,
        fixture_schema="claim-pipeline-effect-adapter-fixtures-v1",
        source_marker="fixture",
        fixture_ref="fixture_case_1",
    )


class DisabledEvidenceSchemaTests(unittest.TestCase):
    def test_gate_1_baseline_constants_match_approved_evidence(self):
        self.assertEqual(
            BASELINE_CODE_REVISION,
            getattr(disabled_evidence_module, "CODE_REVISION", None),
        )
        self.assertEqual(
            BASELINE_EVIDENCE_COMMIT,
            getattr(disabled_evidence_module, "EVIDENCE_COMMIT", None),
        )
        self.assertEqual(
            BASELINE_REPORT_SHA256,
            getattr(disabled_evidence_module, "REPORT_SHA256", None),
        )
        self.assertEqual(
            BASELINE_RESULT_DIGEST,
            getattr(disabled_evidence_module, "RESULT_DIGEST", None),
        )
        self.assertEqual(
            19,
            getattr(disabled_evidence_module, "ISOLATION_TESTS_PASSED", None),
        )

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

    def test_timestamps_require_ordered_rfc3339_utc_values(self):
        self.assertEqual(
            "2026-07-24T12:00:02Z",
            timestamps().captured_at,
        )
        try:
            nanosecond = EvidenceTimestamps(
                evaluation_started_at="2026-07-24T12:00:00.123456789Z",
                evaluation_completed_at="2026-07-24T12:00:00.123456790Z",
                captured_at="2026-07-24T12:00:00.123456791Z",
            )
        except ValueError as exc:
            self.fail(f"valid nanosecond RFC3339 UTC timestamp rejected: {exc}")
        self.assertTrue(nanosecond.captured_at.endswith("791Z"))
        for values in invalid_timestamp_cases():
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    EvidenceTimestamps(**values)

    def test_provenance_requires_closed_fixture_fields_and_bounded_identifiers(self):
        self.assertEqual("fixture", provenance().source_marker)
        for field, value in invalid_provenance_cases():
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    replace(provenance(), **{field: value})
        with self.assertRaises(TypeError):
            EvidenceProvenance(
                code_revision=COMMIT,
                evidence_commit="c" * 40,
                report_sha256="5" * 64,
                result_digest="6" * 64,
                fixture_schema="claim-pipeline-effect-adapter-fixtures-v1",
                source_marker="fixture",
                fixture_ref="fixture_case_1",
                unexpected_field="not-allowed",
            )

    def test_hashes_attestation_and_destination_reject_malformed_values(self):
        for field in (
            "source_sha256",
            "fixture_sha256",
            "projection_sha256",
            "receipt_payload_sha256",
            "payload_sha256",
            "envelope_sha256",
        ):
            with self.subTest(hash_field=field):
                with self.assertRaises(ValueError):
                    replace(content_hashes(), **{field: "A" * 64})

        with self.assertRaises(ValueError):
            legacy_zero_effect_attestation()
        with self.assertRaises(ValueError):
            replace(zero_effect_attestation(), isolation_tests_passed=-1)

        for field, value in invalid_destination_cases():
            with self.subTest(destination_field=field, value=value):
                with self.assertRaises(ValueError):
                    replace(destination_attestation(), **{field: value})

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

    def test_rows_reject_direct_sensitive_or_unbounded_constructor_values(self):
        with self.assertRaises(ValueError):
            EvidenceClaimRow(
                row_id="pat@example.invalid",
                sequence=1,
                claim_ref="claim_ref_" + "1" * 64,
                execution_status="not_applicable_claim",
                disposition=EvidenceDisposition.PROPOSED,
                source_category="fixture",
            )
        with self.assertRaises(ValueError):
            EvidenceClaimRow(
                row_id="row_" + "1" * 64,
                sequence=0,
                claim_ref="claim_ref_" + "1" * 64,
                execution_status="not_applicable_claim",
                disposition=EvidenceDisposition.PROPOSED,
                source_category="fixture",
            )
        with self.assertRaises(ValueError):
            replace(action_row(), action_type="pat@example.invalid")
        with self.assertRaises(ValueError):
            replace(action_row(), action_ref="raw-graph-message-id")
        with self.assertRaises(ValueError):
            replace(action_row(), source_category="live")
        with self.assertRaises(ValueError):
            replace(
                action_row(),
                claim_refs=(
                    "claim_ref_" + "1" * 64,
                    "claim_ref_" + "1" * 64,
                ),
            )

    def test_all_and_only_closed_status_reason_pairs_are_accepted(self):
        valid_pairs = valid_status_reasons()
        for status in DryRunStatus:
            for reason in DryRunReason:
                kwargs = {
                    "sequence": 1,
                    "action_id": "action-closed-lattice",
                    "action_type": "fact_update",
                    "source_claim_ids": ("claim-closed-lattice",),
                    "dependency_action_ids": (),
                    "policy_status": status,
                    "policy_reason": reason,
                    "source_category": "fixture",
                }
                with self.subTest(status=status.value, reason=reason.value):
                    if reason in valid_pairs[status]:
                        row = EvidenceActionRow.create(**kwargs)
                        self.assertEqual(status.value, row.policy_status)
                    else:
                        with self.assertRaises(ValueError):
                            EvidenceActionRow.create(**kwargs)

    def test_envelope_recomputes_summary_and_enforces_row_limit(self):
        first_claim_row = EvidenceClaimRow.create(
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
                rows=(first_claim_row,),
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

        boundary_rows = tuple(claim_row(sequence=index) for index in range(1, 401))
        self.assertEqual(400, len(envelope_with_rows(boundary_rows).rows))

    def test_envelope_rejects_duplicate_foreign_or_out_of_order_rows(self):
        claim = claim_row(sequence=1)
        action = action_row(
            sequence=2,
            claim_refs=(claim.claim_ref,),
        )
        self.assertEqual(2, len(envelope_with_rows((claim, action)).rows))

        with self.assertRaises(ValueError):
            envelope_with_rows((claim, claim))
        with self.assertRaises(ValueError):
            envelope_with_rows((action, claim))
        with self.assertRaises(ValueError):
            envelope_with_rows(
                (
                    action_row(
                        claim_refs=("claim_ref_" + "9" * 64,),
                    ),
                )
            )
        with self.assertRaises(ValueError):
            envelope_with_rows(
                (
                    action_row(
                        dependency_refs=("action_ref_" + "9" * 64,),
                    ),
                )
            )

    def test_contract_subclasses_cannot_add_serialized_fields(self):
        original = claim_row()
        tainted = TaintedClaimRow(
            row_id=original.row_id,
            sequence=original.sequence,
            claim_ref=original.claim_ref,
            execution_status=original.execution_status,
            disposition=original.disposition,
            source_category=original.source_category,
        )
        self.assertIn(
            "pat@example.invalid",
            canonical_json(tainted).decode("ascii"),
        )
        with self.assertRaises(TypeError):
            EvidenceProjection(
                summary=EvidenceSummary(
                    claim_count=1,
                    action_count=0,
                    warning_count=0,
                ),
                rows=(tainted,),
            )

    def test_envelope_rejects_canonical_json_over_256_kib_without_truncation(self):
        rows = oversized_structured_rows()
        self.assertEqual(400, len(rows))
        self.assertGreater(len(canonical_json(rows)), 256 * 1024)
        with self.assertRaises(ValueError):
            envelope_with_rows(rows)

    def test_envelope_accepts_exact_256_kib_boundary_and_rejects_next_byte(self):
        rows = exact_size_boundary_rows()
        boundary_provenance = replace(
            provenance(),
            fixture_ref="fixture_" + "x" * 92,
        )
        boundary = envelope_with_rows(
            rows,
            envelope_provenance=boundary_provenance,
        )
        self.assertEqual(256 * 1024, len(canonical_json(boundary.to_dict())))

        with self.assertRaises(ValueError):
            envelope_with_rows(
                rows,
                envelope_provenance=replace(
                    boundary_provenance,
                    fixture_ref=boundary_provenance.fixture_ref + "x",
                ),
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

    def test_projector_rejects_receipt_that_does_not_match_action_identity(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        effect = receipt.effects[0]
        mismatched_effect = DryRunEffectReceipt.create(
            plan_id=effect.plan_id,
            action_id=effect.action_id,
            idempotency_key="effect_mismatched",
            action_type=effect.action_type,
            sequence=effect.sequence,
            status=effect.status,
            reason=effect.reason,
            dependency_receipt_ids=effect.dependency_receipt_ids,
        )
        mismatched_receipt = DryRunCommitReceipt.create(
            tenant_id=receipt.tenant_id,
            plan_id=receipt.plan_id,
            decision_id=receipt.decision_id,
            contract_id=receipt.contract_id,
            contract_version=receipt.contract_version,
            snapshot_hash=receipt.snapshot_hash,
            effects=(mismatched_effect,),
        )

        with self.assertRaises(ValueError):
            project_disabled_evidence(
                plan=plan,
                claims=(claim,),
                receipt=mismatched_receipt,
            )

    def test_projector_binds_every_commit_receipt_identity_field_to_plan(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        fields = {
            "tenant_id": receipt.tenant_id,
            "plan_id": receipt.plan_id,
            "decision_id": receipt.decision_id,
            "contract_id": receipt.contract_id,
            "contract_version": receipt.contract_version,
            "snapshot_hash": receipt.snapshot_hash,
            "effects": receipt.effects,
        }
        mutations = (
            ("tenant_id", "tenant-mismatch"),
            ("decision_id", "decision-mismatch"),
            ("contract_id", "contract-mismatch"),
            ("contract_version", receipt.contract_version + 1),
            ("snapshot_hash", "snapshot-mismatch"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                mismatched_receipt = DryRunCommitReceipt.create(
                    **{**fields, field: value}
                )
                with self.assertRaises(ValueError):
                    project_disabled_evidence(
                        plan=plan,
                        claims=(claim,),
                        receipt=mismatched_receipt,
                    )

    def test_projector_rejects_duplicate_claims_and_dependency_receipt_mismatch(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        with self.assertRaises(ValueError):
            project_disabled_evidence(
                plan=plan,
                claims=(claim, claim),
                receipt=receipt,
            )

        effect = receipt.effects[0]
        mismatched_effect = DryRunEffectReceipt.create(
            plan_id=effect.plan_id,
            action_id=effect.action_id,
            idempotency_key=effect.idempotency_key,
            action_type=effect.action_type,
            sequence=effect.sequence,
            status=effect.status,
            reason=effect.reason,
            dependency_receipt_ids=("dry_effect_" + "1" * 24,),
        )
        mismatched_receipt = DryRunCommitReceipt.create(
            tenant_id=receipt.tenant_id,
            plan_id=receipt.plan_id,
            decision_id=receipt.decision_id,
            contract_id=receipt.contract_id,
            contract_version=receipt.contract_version,
            snapshot_hash=receipt.snapshot_hash,
            effects=(mismatched_effect,),
        )
        with self.assertRaises(ValueError):
            project_disabled_evidence(
                plan=plan,
                claims=(claim,),
                receipt=mismatched_receipt,
            )

    def test_projector_requires_exact_contract_types_and_bounded_projection(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        plan_fields = {
            field: getattr(plan, field)
            for field in (
                "tenant_id",
                "client_id",
                "campaign_id",
                "plan_id",
                "decision_id",
                "contract_id",
                "contract_version",
                "snapshot_hash",
                "actions",
            )
        }
        receipt_fields = {
            field: getattr(receipt, field)
            for field in (
                "receipt_id",
                "tenant_id",
                "plan_id",
                "decision_id",
                "contract_id",
                "contract_version",
                "snapshot_hash",
                "effects",
            )
        }
        for label, candidate_plan, candidate_claims, candidate_receipt in (
            ("plan", SimpleNamespace(**plan_fields), (claim,), receipt),
            (
                "claim",
                plan,
                (SimpleNamespace(claim_id=claim.claim_id),),
                receipt,
            ),
            (
                "receipt",
                plan,
                (claim,),
                SimpleNamespace(**receipt_fields),
            ),
        ):
            with self.subTest(label=label):
                with self.assertRaises(TypeError):
                    project_disabled_evidence(
                        plan=candidate_plan,
                        claims=candidate_claims,
                        receipt=candidate_receipt,
                    )

        rows = tuple(claim_row(sequence=index) for index in range(1, 402))
        with self.assertRaises(ValueError):
            EvidenceProjection(
                summary=EvidenceSummary(
                    claim_count=401,
                    action_count=0,
                    warning_count=0,
                ),
                rows=rows,
            )


class DisabledEvidenceSerializerTests(unittest.TestCase):
    def test_serializer_is_canonical_and_derives_run_id(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )

        payload = serialize_disabled_evidence(
            plan=plan,
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=zero_effect_attestation(),
        )
        envelope = bind_fixture_evidence_envelope(
            payload,
            plan=plan,
            receipt=receipt,
            trust_anchor=fixture_trust_anchor(),
        )
        repeated_payload = serialize_disabled_evidence(
            plan=plan,
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=zero_effect_attestation(),
        )
        repeated = bind_fixture_evidence_envelope(
            repeated_payload,
            plan=plan,
            receipt=receipt,
            trust_anchor=fixture_trust_anchor(),
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
        self.assertNotIn("destination_attestation", payload.to_dict())
        self.assertEqual(
            destination_attestation(),
            envelope.destination_attestation,
        )
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
                plan=plan,
                receipt=receipt,
                projection=projection,
                provenance=provenance(),
                timestamps=timestamps(),
                zero_effect_attestation=zero_effect_attestation(),
                run_id="run_caller_selected",
            )

    def test_serializer_rejects_projection_not_bound_to_receipt(self):
        _, plan, receipt, projection = projection_and_receipt()
        foreign_action = replace(
            projection.rows[1],
            action_ref="action_ref_" + "9" * 64,
        )
        foreign_projection = EvidenceProjection(
            summary=projection.summary,
            rows=(projection.rows[0], foreign_action),
        )
        with self.assertRaises(ValueError):
            serialize_disabled_evidence(
                plan=plan,
                receipt=receipt,
                projection=foreign_projection,
                provenance=provenance(),
                timestamps=timestamps(),
                zero_effect_attestation=zero_effect_attestation(),
            )

    def test_serializer_does_not_accept_caller_destination_identity(self):
        _, plan, receipt, projection = projection_and_receipt()
        with self.assertRaises(TypeError):
            serialize_disabled_evidence(
                plan=plan,
                receipt=receipt,
                projection=projection,
                provenance=provenance(),
                timestamps=timestamps(),
                zero_effect_attestation=zero_effect_attestation(),
                destination_attestation=destination_attestation(),
            )

    def test_serializer_rejects_extra_or_foreign_claim_rows(self):
        _, plan, receipt, projection = projection_and_receipt()
        original_claim, original_action = projection.rows
        extra_claim = EvidenceClaimRow.create(
            sequence=2,
            claim_id="claim-extra",
            source_category="fixture",
        )
        extra_projection = EvidenceProjection(
            summary=EvidenceSummary(
                claim_count=2,
                action_count=1,
                warning_count=0,
            ),
            rows=(original_claim, extra_claim, original_action),
        )
        foreign_claim = EvidenceClaimRow.create(
            sequence=1,
            claim_id="claim-foreign",
            source_category="fixture",
        )
        foreign_action = EvidenceActionRow.create(
            sequence=original_action.sequence,
            action_id=plan.actions[0].action_id,
            action_type=original_action.action_type,
            source_claim_ids=("claim-foreign",),
            dependency_action_ids=(),
            policy_status=receipt.effects[0].status,
            policy_reason=receipt.effects[0].reason,
            source_category="fixture",
        )
        foreign_projection = EvidenceProjection(
            summary=projection.summary,
            rows=(foreign_claim, foreign_action),
        )

        for candidate in (extra_projection, foreign_projection):
            with self.subTest(rows=candidate.rows):
                with self.assertRaises(ValueError):
                    serialize_disabled_evidence(
                        plan=plan,
                        receipt=receipt,
                        projection=candidate,
                        provenance=provenance(),
                        timestamps=timestamps(),
                        zero_effect_attestation=zero_effect_attestation(),
                    )

    def test_payload_versions_and_trusted_binding_fail_closed(self):
        _, plan, receipt, projection = projection_and_receipt()
        payload = serialize_disabled_evidence(
            plan=plan,
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=zero_effect_attestation(),
        )
        for field, value in (
            ("schema_version", "sitesift-disabled-evidence-v2"),
            ("taxonomy_version", "sitesift-evidence-disposition-v2"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    replace(payload, **{field: value})
                bypassed_payload = object.__new__(type(payload))
                for stored_field, stored_value in payload.__dict__.items():
                    object.__setattr__(
                        bypassed_payload,
                        stored_field,
                        value if stored_field == field else stored_value,
                    )
                with self.assertRaises(ValueError):
                    bind_fixture_evidence_envelope(
                        bypassed_payload,
                        plan=plan,
                        receipt=receipt,
                        trust_anchor=fixture_trust_anchor(),
                    )

        forged_receipt_hash = "9" * 64
        forged_basis = disabled_evidence_module._payload_basis_values(
            schema_version=payload.schema_version,
            run_id=payload.run_id,
            taxonomy_version=payload.taxonomy_version,
            provenance=payload.provenance,
            timestamps=payload.timestamps,
            projection_sha256=payload.content_hashes.projection_sha256,
            receipt_payload_sha256=forged_receipt_hash,
            zero_effect_attestation=payload.zero_effect_attestation,
            summary=payload.summary,
            rows=payload.rows,
        )
        forged_payload = replace(
            payload,
            content_hashes=replace(
                payload.content_hashes,
                receipt_payload_sha256=forged_receipt_hash,
                payload_sha256=hashlib.sha256(
                    canonical_json(forged_basis)
                ).hexdigest(),
            ),
        )
        with self.assertRaises(ValueError):
            bind_fixture_evidence_envelope(
                forged_payload,
                plan=plan,
                receipt=receipt,
                trust_anchor=fixture_trust_anchor(),
            )

        forged_attestation = replace(
            payload.zero_effect_attestation,
            test_manifest_sha256="8" * 64,
            signature="0" * 64,
        )
        unsigned_attestation = {
            key: value
            for key, value in forged_attestation.__dict__.items()
            if key != "signature"
        }
        forged_attestation = replace(
            forged_attestation,
            signature=fixture_signature(unsigned_attestation),
        )
        forged_attestation_payload = serialize_disabled_evidence(
            plan=plan,
            receipt=receipt,
            projection=projection,
            provenance=provenance(),
            timestamps=timestamps(),
            zero_effect_attestation=forged_attestation,
        )
        with self.assertRaises(ValueError):
            bind_fixture_evidence_envelope(
                forged_attestation_payload,
                plan=plan,
                receipt=receipt,
                trust_anchor=fixture_trust_anchor(),
            )
        forged_anchor = FixtureTrustAnchor(
            verifier_id=forged_attestation.verifier_id,
            verifier_version=forged_attestation.verifier_version,
            signature=forged_attestation.signature,
        )
        with self.assertRaises(ValueError):
            bind_fixture_evidence_envelope(
                forged_attestation_payload,
                plan=plan,
                receipt=receipt,
                trust_anchor=forged_anchor,
            )

        valid_envelope = reserialized_envelope(
            plan=plan,
            receipt=receipt,
            projection=projection,
            attestation=zero_effect_attestation(),
        )
        forged_envelope = replace(
            valid_envelope,
            zero_effect_attestation=forged_attestation,
        )
        forged_result = verify_disabled_evidence_envelope(
            forged_envelope,
            receipt=receipt,
            trust_anchor=forged_anchor,
        )
        self.assertEqual(
            "invalid_zero_effect_attestation",
            forged_result.failure_code,
        )

    def test_envelope_canonical_shape_flattens_provenance_fields(self):
        _, plan, receipt, projection = projection_and_receipt()
        envelope = reserialized_envelope(
            plan=plan,
            receipt=receipt,
            projection=projection,
            attestation=zero_effect_attestation(),
        )
        serialized = envelope.to_dict()
        self.assertNotIn("provenance", serialized)
        for field_name in (
            "code_revision",
            "evidence_commit",
            "report_sha256",
            "result_digest",
            "fixture_schema",
            "source_marker",
            "fixture_ref",
        ):
            self.assertEqual(
                getattr(envelope.provenance, field_name),
                serialized[field_name],
            )


class DisabledEvidenceVerifierTests(unittest.TestCase):
    def test_verifier_requires_independent_zero_effect_attestation(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )
        envelope = reserialized_envelope(
            plan=plan,
            receipt=receipt,
            projection=projection,
            attestation=zero_effect_attestation(),
        )

        verified = verify_disabled_evidence_envelope(
            envelope,
            receipt=receipt,
            trust_anchor=fixture_trust_anchor(),
        )
        self.assertTrue(verified.verified)
        self.assertTrue(verified.include_in_normal_reads)

        with self.assertRaises(TypeError):
            verify_disabled_evidence_envelope(
                receipt,
                receipt=receipt,
                trust_anchor=fixture_trust_anchor(),
            )

        missing = replace(envelope, zero_effect_attestation=None)
        missing_result = verify_disabled_evidence_envelope(
            missing,
            receipt=receipt,
            trust_anchor=fixture_trust_anchor(),
        )
        self.assertFalse(missing_result.verified)
        self.assertFalse(missing_result.include_in_normal_reads)
        self.assertEqual("missing_zero_effect_attestation", missing_result.failure_code)

        forged = replace(
            envelope,
            zero_effect_attestation=replace(
                envelope.zero_effect_attestation,
                signature="0" * 64,
            ),
        )
        forged_result = verify_disabled_evidence_envelope(
            forged,
            receipt=receipt,
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
            receipt=receipt,
            trust_anchor=fixture_trust_anchor(),
        )
        self.assertFalse(taxonomy_result.verified)
        self.assertFalse(taxonomy_result.include_in_normal_reads)
        self.assertEqual("unsupported_taxonomy", taxonomy_result.failure_code)
        self.assertEqual(
            EvidenceDisposition.UNKNOWN_TAXONOMY,
            taxonomy_result.warning_disposition,
        )

    def test_verifier_recomputes_canonical_integrity_before_accepting_envelope(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )
        envelope = reserialized_envelope(
            plan=plan,
            receipt=receipt,
            projection=projection,
            attestation=zero_effect_attestation(),
        )

        tampered_projection = replace(
            envelope,
            summary=EvidenceSummary(claim_count=0, action_count=0, warning_count=0),
            rows=(),
        )
        tampered_run = replace(envelope, run_id="run_" + "0" * 64)
        tampered_payload_hash = replace(
            envelope,
            content_hashes=replace(
                envelope.content_hashes,
                payload_sha256="0" * 64,
            ),
        )
        tampered_time = replace(
            envelope,
            timestamps=replace(
                envelope.timestamps,
                captured_at="2026-07-24T12:00:03Z",
            ),
        )
        tampered_provenance = replace(
            envelope,
            provenance=replace(envelope.provenance, result_digest="9" * 64),
        )

        for label, candidate in (
            ("projection", tampered_projection),
            ("run_id", tampered_run),
            ("payload_hash", tampered_payload_hash),
            ("timestamp", tampered_time),
            ("provenance", tampered_provenance),
        ):
            with self.subTest(label=label):
                result = verify_disabled_evidence_envelope(
                    candidate,
                    receipt=receipt,
                    trust_anchor=fixture_trust_anchor(),
                )
                self.assertFalse(result.verified)
                self.assertFalse(result.include_in_normal_reads)
                self.assertEqual(
                    (
                        "invalid_provenance"
                        if label == "provenance"
                        else "hash_integrity_mismatch"
                    ),
                    result.failure_code,
                )

    def test_verifier_rejects_each_rehashed_attestation_forgery(self):
        _, plan, receipt, projection = projection_and_receipt()
        original_attestation = zero_effect_attestation()
        original_provenance = provenance()
        cases = (
            (
                "test_manifest",
                replace(original_attestation, test_manifest_sha256="9" * 64),
                original_provenance,
            ),
            (
                "isolation_count",
                replace(original_attestation, isolation_tests_passed=20),
                original_provenance,
            ),
            (
                "verification_run",
                replace(
                    original_attestation,
                    verification_run_id="verification-run-2",
                ),
                original_provenance,
            ),
            (
                "report",
                replace(original_attestation, verified_report_sha256="8" * 64),
                replace(original_provenance, report_sha256="8" * 64),
            ),
            (
                "result",
                replace(original_attestation, verified_result_digest="7" * 64),
                replace(original_provenance, result_digest="7" * 64),
            ),
        )
        for label, forged_attestation, forged_provenance in cases:
            with self.subTest(label=label):
                payload = serialize_disabled_evidence(
                    plan=plan,
                    receipt=receipt,
                    projection=projection,
                    provenance=forged_provenance,
                    timestamps=timestamps(),
                    zero_effect_attestation=forged_attestation,
                )
                with self.assertRaises(ValueError):
                    bind_fixture_evidence_envelope(
                        payload,
                        plan=plan,
                        receipt=receipt,
                        trust_anchor=fixture_trust_anchor(),
                    )

                candidate = replace(
                    reserialized_envelope(
                        plan=plan,
                        receipt=receipt,
                        projection=projection,
                        attestation=original_attestation,
                    ),
                    zero_effect_attestation=forged_attestation,
                    provenance=forged_provenance,
                )
                result = verify_disabled_evidence_envelope(
                    candidate,
                    receipt=receipt,
                    trust_anchor=fixture_trust_anchor(),
                )
                self.assertFalse(result.verified)
                self.assertFalse(result.include_in_normal_reads)
                self.assertEqual(
                    "invalid_zero_effect_attestation",
                    result.failure_code,
                )

    def test_verifier_classifies_unknown_schema_before_hash_verification(self):
        _, plan, receipt, projection = projection_and_receipt()
        envelope = reserialized_envelope(
            plan=plan,
            receipt=receipt,
            projection=projection,
            attestation=zero_effect_attestation(),
        )
        unsupported_schema = replace(
            envelope,
            schema_version="sitesift-disabled-evidence-v2",
        )

        result = verify_disabled_evidence_envelope(
            unsupported_schema,
            receipt=receipt,
            trust_anchor=fixture_trust_anchor(),
        )

        self.assertFalse(result.verified)
        self.assertFalse(result.include_in_normal_reads)
        self.assertEqual("unsupported_schema", result.failure_code)
        self.assertEqual(
            EvidenceDisposition.UNKNOWN_TAXONOMY,
            result.warning_disposition,
        )

    def test_verifier_rejects_rehashed_unapproved_code_or_evidence_revision(self):
        _, plan, receipt, projection = projection_and_receipt()
        mutations = (
            ("code_revision", "a" * 40),
            ("evidence_commit", "b" * 40),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                unapproved_provenance = replace(
                    provenance(),
                    **{field: value},
                )
                payload = serialize_disabled_evidence(
                    plan=plan,
                    receipt=receipt,
                    projection=projection,
                    provenance=unapproved_provenance,
                    timestamps=timestamps(),
                    zero_effect_attestation=zero_effect_attestation(),
                )
                with self.assertRaises(ValueError):
                    bind_fixture_evidence_envelope(
                        payload,
                        plan=plan,
                        receipt=receipt,
                        trust_anchor=fixture_trust_anchor(),
                    )

                candidate = replace(
                    reserialized_envelope(
                        plan=plan,
                        receipt=receipt,
                        projection=projection,
                        attestation=zero_effect_attestation(),
                    ),
                    provenance=unapproved_provenance,
                )
                result = verify_disabled_evidence_envelope(
                    candidate,
                    receipt=receipt,
                    trust_anchor=fixture_trust_anchor(),
                )
                self.assertFalse(result.verified)
                self.assertFalse(result.include_in_normal_reads)
                self.assertEqual("invalid_provenance", result.failure_code)


class DisabledEvidenceDuplicateTests(unittest.TestCase):
    def test_same_run_id_different_envelope_hash_conflicts_without_overwrite(self):
        claim, plan, receipt = tainted_plan_and_receipt()
        projection = project_disabled_evidence(
            plan=plan,
            claims=(claim,),
            receipt=receipt,
        )
        original = reserialized_envelope(
            plan=plan,
            receipt=receipt,
            projection=projection,
            attestation=zero_effect_attestation(),
        )
        changed = replace(
            original,
            summary=EvidenceSummary(claim_count=0, action_count=0, warning_count=0),
            rows=(),
        )
        changed_same_stored_hash = replace(
            original,
            summary=EvidenceSummary(claim_count=0, action_count=0, warning_count=0),
            rows=(),
        )
        changed_different_stored_hash = replace(
            original,
            content_hashes=replace(
                original.content_hashes,
                envelope_sha256="0" * 64,
            ),
        )

        same = classify_duplicate_envelope(original, original)
        conflict = classify_duplicate_envelope(original, changed)
        conflict_same_stored_hash = classify_duplicate_envelope(
            original,
            changed_same_stored_hash,
        )
        conflict_different_stored_hash = classify_duplicate_envelope(
            original,
            changed_different_stored_hash,
        )

        self.assertEqual("same_hash_duplicate", same.outcome)
        self.assertFalse(same.should_write)
        self.assertEqual("conflict", conflict.outcome)
        self.assertFalse(conflict.should_write)
        self.assertTrue(conflict.preserve_original)
        self.assertEqual("conflict", conflict_same_stored_hash.outcome)
        self.assertFalse(conflict_same_stored_hash.should_write)
        self.assertEqual("conflict", conflict_different_stored_hash.outcome)
        self.assertFalse(conflict_different_stored_hash.should_write)

        for field_name in (
            "source_sha256",
            "fixture_sha256",
            "projection_sha256",
            "payload_sha256",
        ):
            with self.subTest(hash_field=field_name):
                tampered_hash_bundle = replace(
                    original,
                    content_hashes=replace(
                        original.content_hashes,
                        **{field_name: "0" * 64},
                    ),
                )
                result = classify_duplicate_envelope(
                    original,
                    tampered_hash_bundle,
                )
                self.assertEqual("conflict", result.outcome)
                self.assertFalse(result.should_write)
                self.assertTrue(result.preserve_original)


if __name__ == "__main__":
    unittest.main()
