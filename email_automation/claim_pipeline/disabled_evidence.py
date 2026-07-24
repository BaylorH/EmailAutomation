"""Pure disabled-staging evidence contracts for SiteSift Gate 1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum

from .effect_adapter import DryRunReason, DryRunStatus

SCHEMA_VERSION = "sitesift-disabled-evidence-v1"
TAXONOMY_VERSION = "sitesift-evidence-disposition-v1"
ZERO_EFFECT_ATTESTATION_SCHEMA = "sitesift-zero-effect-attestation-v1"
SOURCE_SHA256 = "b391407cd5bbc673874eb80bbf488ba4cfdb04b285daaf45cd34f0bf6052d634"
FIXTURE_SHA256 = "c654da2a9a2fadee2f8cce761e17dd78d21168cfcb6b6e9fd06aeddff69ea229"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        _json_ready(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(value: object) -> bytes:
    return _json_bytes(value)


def _stable_ref(prefix: str, domain: str, payload: object) -> str:
    digest = hashlib.sha256(
        domain.encode("utf-8") + b"\0" + _json_bytes(payload)
    ).hexdigest()
    return f"{prefix}_{digest}"


def _enum_value(value: object) -> str:
    return getattr(value, "value", str(value))


def _json_ready(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_ready(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    return value


class _JsonContract:
    def to_dict(self) -> dict[str, object]:
        return _json_ready(self)


class EvidenceDisposition(str, Enum):
    PROPOSED = "proposed"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    BLOCKED_BY_DISABLED_ADAPTER = "blocked_by_disabled_adapter"
    INVALID_INPUT = "invalid_input"
    UNKNOWN_TAXONOMY = "unknown_taxonomy"


@dataclass(frozen=True)
class EvidenceTimestamps:
    evaluation_started_at: str
    evaluation_completed_at: str
    captured_at: str


@dataclass(frozen=True)
class EvidenceContentHashes:
    source_sha256: str
    fixture_sha256: str
    projection_sha256: str
    receipt_payload_sha256: str
    payload_sha256: str
    envelope_sha256: str


@dataclass(frozen=True)
class ZeroEffectAttestation:
    verified_source_sha256: str
    verified_report_sha256: str
    verified_result_digest: str
    test_manifest_sha256: str
    isolation_tests_passed: int
    verifier_id: str
    verifier_version: str
    verification_run_id: str
    signature: str
    attestation_schema: str = "sitesift-zero-effect-attestation-v1"


@dataclass(frozen=True)
class FixtureTrustAnchor:
    verifier_id: str
    verifier_version: str
    signature: str


@dataclass(frozen=True)
class EvidenceVerificationResult:
    verified: bool
    include_in_normal_reads: bool
    failure_code: str = ""
    warning_disposition: EvidenceDisposition | None = None


@dataclass(frozen=True)
class EvidenceDuplicateResult:
    outcome: str
    should_write: bool
    preserve_original: bool = False


@dataclass(frozen=True)
class EvidenceDestinationAttestation:
    environment: str
    project_or_store: str
    namespace: str
    deployment_identity_sha256: str


@dataclass(frozen=True)
class EvidenceProvenance:
    code_revision: str
    evidence_commit: str
    report_sha256: str
    result_digest: str
    fixture_schema: str
    source_marker: str
    fixture_ref: str


@dataclass(frozen=True)
class EvidenceSummary(_JsonContract):
    claim_count: int
    action_count: int
    warning_count: int


_ACTION_DISPOSITION_BY_STATUS = {
    DryRunStatus.WOULD_APPLY.value: (
        "not_attempted_adapter_disabled",
        EvidenceDisposition.BLOCKED_BY_DISABLED_ADAPTER,
    ),
    DryRunStatus.BLOCKED.value: (
        "not_attempted_policy_gate",
        EvidenceDisposition.BLOCKED_BY_POLICY,
    ),
    DryRunStatus.SKIPPED.value: (
        "not_attempted_policy_gate",
        EvidenceDisposition.BLOCKED_BY_POLICY,
    ),
}
_PERSISTED_ACTION_DISPOSITIONS = frozenset(
    {
        EvidenceDisposition.BLOCKED_BY_POLICY,
        EvidenceDisposition.BLOCKED_BY_DISABLED_ADAPTER,
    }
)
_VALID_REASONS_BY_STATUS = {
    DryRunStatus.WOULD_APPLY.value: frozenset(
        {
            DryRunReason.ELIGIBLE_AUTOMATIC_ACTION.value,
            DryRunReason.ELIGIBLE_HUMAN_APPROVED_ACTION.value,
        }
    ),
    DryRunStatus.SKIPPED.value: frozenset(
        {
            DryRunReason.APPROVAL_REQUIRED.value,
            DryRunReason.IDEMPOTENCY_KEY_ALREADY_COMMITTED.value,
        }
    ),
    DryRunStatus.BLOCKED.value: frozenset(
        {
            DryRunReason.APPROVAL_SCOPE_MISMATCH.value,
            DryRunReason.UNSUPPORTED_ACTION_TYPE.value,
            DryRunReason.STALE_SNAPSHOT.value,
            DryRunReason.STALE_CONTRACT.value,
            DryRunReason.PRIOR_STATE_MISMATCH.value,
            DryRunReason.DEPENDENCY_BLOCKED.value,
            DryRunReason.TERMINAL_OUTBOUND_SUPPRESSED.value,
            DryRunReason.PLAN_CONTRACT_VIOLATION.value,
        }
    ),
}


@dataclass(frozen=True)
class EvidenceClaimRow(_JsonContract):
    row_id: str
    sequence: int
    claim_ref: str
    execution_status: str
    disposition: EvidenceDisposition
    source_category: str

    def __post_init__(self) -> None:
        if self.execution_status != "not_applicable_claim":
            raise ValueError("claim rows must use not_applicable_claim execution status")
        if self.disposition is not EvidenceDisposition.PROPOSED:
            raise ValueError("claim rows must use proposed disposition")

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        claim_id: str,
        source_category: str,
    ) -> "EvidenceClaimRow":
        claim_ref = _stable_ref(
            "claim_ref",
            "sitesift-disabled-evidence-claim-ref-v1",
            {"claim_id": claim_id},
        )
        return cls(
            row_id=_stable_ref(
                "row",
                "sitesift-disabled-evidence-row-v1",
                {"row_kind": "claim", "sequence": sequence, "claim_ref": claim_ref},
            ),
            sequence=sequence,
            claim_ref=claim_ref,
            execution_status="not_applicable_claim",
            disposition=EvidenceDisposition.PROPOSED,
            source_category=source_category,
        )


@dataclass(frozen=True)
class EvidenceActionRow(_JsonContract):
    row_id: str
    sequence: int
    action_ref: str
    action_type: str
    claim_refs: tuple[str, ...]
    dependency_refs: tuple[str, ...]
    policy_status: str
    policy_reason: str
    execution_status: str
    disposition: EvidenceDisposition
    source_category: str

    def __post_init__(self) -> None:
        if self.disposition not in _PERSISTED_ACTION_DISPOSITIONS:
            raise ValueError("action row disposition is not persistable")
        if self.policy_status not in _ACTION_DISPOSITION_BY_STATUS:
            raise ValueError("unknown policy status")
        expected_execution, expected_disposition = _ACTION_DISPOSITION_BY_STATUS[
            self.policy_status
        ]
        if self.execution_status != expected_execution:
            raise ValueError("action row execution status does not match policy status")
        if self.disposition is not expected_disposition:
            raise ValueError("action row disposition does not match policy status")
        if self.policy_reason not in {reason.value for reason in DryRunReason}:
            raise ValueError("unknown policy reason")
        if self.policy_reason not in _VALID_REASONS_BY_STATUS[self.policy_status]:
            raise ValueError("invalid policy status/reason combination")

    @classmethod
    def create(
        cls,
        *,
        sequence: int,
        action_id: str,
        action_type: str,
        source_claim_ids: tuple[str, ...],
        dependency_action_ids: tuple[str, ...],
        policy_status: object,
        policy_reason: object,
        source_category: str,
    ) -> "EvidenceActionRow":
        normalized_status = _enum_value(policy_status)
        if normalized_status not in _ACTION_DISPOSITION_BY_STATUS:
            raise ValueError("unknown policy status")
        execution_status, disposition = _ACTION_DISPOSITION_BY_STATUS[normalized_status]
        action_ref = _stable_ref(
            "action_ref",
            "sitesift-disabled-evidence-action-ref-v1",
            {"action_id": action_id},
        )
        claim_refs = tuple(
            _stable_ref(
                "claim_ref",
                "sitesift-disabled-evidence-claim-ref-v1",
                {"claim_id": claim_id},
            )
            for claim_id in source_claim_ids
        )
        dependency_refs = tuple(
            _stable_ref(
                "action_ref",
                "sitesift-disabled-evidence-action-ref-v1",
                {"action_id": action_id},
            )
            for action_id in dependency_action_ids
        )
        return cls(
            row_id=_stable_ref(
                "row",
                "sitesift-disabled-evidence-row-v1",
                {
                    "row_kind": "action",
                    "sequence": sequence,
                    "action_ref": action_ref,
                },
            ),
            sequence=sequence,
            action_ref=action_ref,
            action_type=action_type,
            claim_refs=claim_refs,
            dependency_refs=dependency_refs,
            policy_status=normalized_status,
            policy_reason=_enum_value(policy_reason),
            execution_status=execution_status,
            disposition=disposition,
            source_category=source_category,
        )


@dataclass(frozen=True)
class EvidenceProjection(_JsonContract):
    summary: EvidenceSummary
    rows: tuple[EvidenceClaimRow | EvidenceActionRow, ...]


def derive_run_id(
    *,
    receipt_id: str,
    projection_sha256: str,
    fixture_sha256: str,
    code_revision: str,
    result_digest: str,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    payload = {
        "schema_version": schema_version,
        "receipt_id": receipt_id,
        "projection_sha256": projection_sha256,
        "fixture_sha256": fixture_sha256,
        "code_revision": code_revision,
        "result_digest": result_digest,
    }
    digest = hashlib.sha256(
        b"sitesift-disabled-evidence-run-v1\0" + canonical_json(payload)
    ).hexdigest()
    return f"run_{digest}"


def project_disabled_evidence(
    *,
    plan: object,
    claims: tuple[object, ...],
    receipt: object,
) -> EvidenceProjection:
    if getattr(receipt, "plan_id", None) != getattr(plan, "plan_id", None):
        raise ValueError("receipt plan_id must match plan plan_id")
    claims_by_id = {getattr(claim, "claim_id", None): claim for claim in claims}
    if None in claims_by_id:
        raise ValueError("claims must expose claim_id")

    actions = tuple(getattr(plan, "actions", ()))
    actions_by_id = {getattr(action, "action_id", None): action for action in actions}
    if None in actions_by_id:
        raise ValueError("plan actions must expose action_id")
    effects = tuple(getattr(receipt, "effects", ()))
    effects_by_action = {
        getattr(effect, "action_id", None): effect
        for effect in effects
    }
    if set(effects_by_action) != set(actions_by_id):
        raise ValueError("receipt effects must match plan actions exactly")

    referenced_claim_ids = tuple(
        dict.fromkeys(
            claim_id
            for action in actions
            for claim_id in getattr(action, "source_claim_ids", ())
        )
    )
    missing_claims = tuple(
        claim_id for claim_id in referenced_claim_ids if claim_id not in claims_by_id
    )
    if missing_claims:
        raise ValueError("plan references claims not supplied to projector")

    claim_rows = tuple(
        EvidenceClaimRow.create(
            sequence=index,
            claim_id=claim_id,
            source_category="fixture",
        )
        for index, claim_id in enumerate(referenced_claim_ids, start=1)
    )
    action_rows = tuple(
        EvidenceActionRow.create(
            sequence=action.sequence,
            action_id=action.action_id,
            action_type=action.action_type.value,
            source_claim_ids=tuple(action.source_claim_ids),
            dependency_action_ids=tuple(action.dependencies),
            policy_status=effects_by_action[action.action_id].status,
            policy_reason=effects_by_action[action.action_id].reason,
            source_category="fixture",
        )
        for action in sorted(actions, key=lambda item: (item.sequence, item.action_id))
    )
    return EvidenceProjection(
        summary=EvidenceSummary(
            claim_count=len(claim_rows),
            action_count=len(action_rows),
            warning_count=0,
        ),
        rows=claim_rows + action_rows,
    )


@dataclass(frozen=True)
class DisabledEvidenceEnvelope(_JsonContract):
    run_id: str
    taxonomy_version: str
    provenance: EvidenceProvenance
    adapter_mode: str
    environment_marker: str
    timestamps: EvidenceTimestamps
    content_hashes: EvidenceContentHashes
    zero_effect_attestation: ZeroEffectAttestation
    destination_attestation: EvidenceDestinationAttestation
    summary: EvidenceSummary
    rows: tuple[object, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.adapter_mode != "disabled":
            raise ValueError("adapter_mode must be exactly disabled")
        object.__setattr__(self, "rows", tuple(self.rows))
        if len(self.rows) > 400:
            raise ValueError("evidence envelope rows exceed 400 row limit")
        if not all(
            isinstance(row, (EvidenceClaimRow, EvidenceActionRow))
            for row in self.rows
        ):
            raise TypeError("evidence envelope rows must be evidence row values")
        expected_summary = EvidenceSummary(
            claim_count=sum(isinstance(row, EvidenceClaimRow) for row in self.rows),
            action_count=sum(isinstance(row, EvidenceActionRow) for row in self.rows),
            warning_count=0,
        )
        if self.summary != expected_summary:
            raise ValueError("evidence envelope summary must match rows")


def serialize_disabled_evidence(
    *,
    receipt: object,
    projection: EvidenceProjection,
    provenance: EvidenceProvenance,
    timestamps: EvidenceTimestamps,
    zero_effect_attestation: ZeroEffectAttestation,
    destination_attestation: EvidenceDestinationAttestation,
) -> DisabledEvidenceEnvelope:
    projection_sha256 = hashlib.sha256(
        canonical_json(projection.to_dict())
    ).hexdigest()
    receipt_payload_sha256 = hashlib.sha256(
        canonical_json(receipt.to_dict())
    ).hexdigest()
    run_id = derive_run_id(
        receipt_id=receipt.receipt_id,
        projection_sha256=projection_sha256,
        fixture_sha256=FIXTURE_SHA256,
        code_revision=provenance.code_revision,
        result_digest=provenance.result_digest,
    )
    payload_basis = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "taxonomy_version": TAXONOMY_VERSION,
        "provenance": provenance,
        "adapter_mode": "disabled",
        "environment_marker": "local_fixture",
        "timestamps": timestamps,
        "content_hashes": {
            "source_sha256": SOURCE_SHA256,
            "fixture_sha256": FIXTURE_SHA256,
            "projection_sha256": projection_sha256,
            "receipt_payload_sha256": receipt_payload_sha256,
        },
        "zero_effect_attestation": zero_effect_attestation,
        "summary": projection.summary,
        "rows": projection.rows,
    }
    payload_sha256 = hashlib.sha256(canonical_json(payload_basis)).hexdigest()
    envelope_basis = dict(payload_basis)
    envelope_basis["destination_attestation"] = destination_attestation
    envelope_basis["content_hashes"] = {
        **payload_basis["content_hashes"],
        "payload_sha256": payload_sha256,
    }
    envelope_sha256 = hashlib.sha256(canonical_json(envelope_basis)).hexdigest()
    hashes = EvidenceContentHashes(
        source_sha256=SOURCE_SHA256,
        fixture_sha256=FIXTURE_SHA256,
        projection_sha256=projection_sha256,
        receipt_payload_sha256=receipt_payload_sha256,
        payload_sha256=payload_sha256,
        envelope_sha256=envelope_sha256,
    )
    return DisabledEvidenceEnvelope(
        run_id=run_id,
        taxonomy_version=TAXONOMY_VERSION,
        provenance=provenance,
        adapter_mode="disabled",
        environment_marker="local_fixture",
        timestamps=timestamps,
        content_hashes=hashes,
        zero_effect_attestation=zero_effect_attestation,
        destination_attestation=destination_attestation,
        summary=projection.summary,
        rows=projection.rows,
    )


def verify_disabled_evidence_envelope(
    envelope: object,
    *,
    trust_anchor: FixtureTrustAnchor,
) -> EvidenceVerificationResult:
    if not isinstance(envelope, DisabledEvidenceEnvelope):
        raise TypeError("verifier requires a DisabledEvidenceEnvelope")
    if envelope.taxonomy_version != TAXONOMY_VERSION:
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="unsupported_taxonomy",
            warning_disposition=EvidenceDisposition.UNKNOWN_TAXONOMY,
        )
    attestation = envelope.zero_effect_attestation
    if not isinstance(attestation, ZeroEffectAttestation):
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="missing_zero_effect_attestation",
        )
    expected = (
        attestation.attestation_schema == ZERO_EFFECT_ATTESTATION_SCHEMA
        and attestation.verified_source_sha256
        == envelope.content_hashes.source_sha256
        and attestation.verified_report_sha256 == envelope.provenance.report_sha256
        and attestation.verified_result_digest == envelope.provenance.result_digest
        and attestation.verifier_id == trust_anchor.verifier_id
        and attestation.verifier_version == trust_anchor.verifier_version
        and attestation.signature == trust_anchor.signature
    )
    if not expected:
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="invalid_zero_effect_attestation",
        )
    return EvidenceVerificationResult(verified=True, include_in_normal_reads=True)


def classify_duplicate_envelope(
    existing: DisabledEvidenceEnvelope,
    candidate: DisabledEvidenceEnvelope,
) -> EvidenceDuplicateResult:
    if not isinstance(existing, DisabledEvidenceEnvelope) or not isinstance(
        candidate,
        DisabledEvidenceEnvelope,
    ):
        raise TypeError("duplicate classification requires evidence envelopes")
    if existing.run_id != candidate.run_id:
        return EvidenceDuplicateResult(outcome="new_run", should_write=True)
    if (
        existing.content_hashes.envelope_sha256
        == candidate.content_hashes.envelope_sha256
    ):
        return EvidenceDuplicateResult(
            outcome="same_hash_duplicate",
            should_write=False,
            preserve_original=True,
        )
    return EvidenceDuplicateResult(
        outcome="conflict",
        should_write=False,
        preserve_original=True,
    )
