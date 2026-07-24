"""Pure disabled-staging evidence contracts for SiteSift Gate 1."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum

from .contracts import ActionPlan, ActionType, Claim, PlannedAction
from .effect_adapter import (
    DryRunCommitReceipt,
    DryRunEffectReceipt,
    DryRunReason,
    DryRunStatus,
)

SCHEMA_VERSION = "sitesift-disabled-evidence-v1"
TAXONOMY_VERSION = "sitesift-evidence-disposition-v1"
ZERO_EFFECT_ATTESTATION_SCHEMA = "sitesift-zero-effect-attestation-v1"
FIXTURE_SCHEMA = "claim-pipeline-effect-adapter-fixtures-v1"
CODE_REVISION = "5a09a67729fb3054298a92cebf40937056c48647"
EVIDENCE_COMMIT = "df8425269c1ce3ab9bc4611705706d78c39dff02"
REPORT_SHA256 = "33103b700cebe55133d3d97a6dba8743a3961cd49040e88e8807c8d5bbc9c7b2"
RESULT_DIGEST = "450124af49e8c7827ee14ca99cdc13056865103a771a7028b20fb9b1ada63d7e"
ISOLATION_TESTS_PASSED = 19
SOURCE_SHA256 = "b391407cd5bbc673874eb80bbf488ba4cfdb04b285daaf45cd34f0bf6052d634"
FIXTURE_SHA256 = "c654da2a9a2fadee2f8cce761e17dd78d21168cfcb6b6e9fd06aeddff69ea229"
TRUSTED_FIXTURE_VERIFIER_ID = "fixture-verifier"
TRUSTED_FIXTURE_VERIFIER_VERSION = "fixture-v1"
TRUSTED_FIXTURE_ATTESTATION_SIGNATURE = (
    "070e099370cf275ff802c0f50821b329fb695f0c22b8da2bcb49b312a07c4b3c"
)
FIXTURE_DESTINATION_ENVIRONMENT = "local_fixture"
FIXTURE_PROJECT_OR_STORE = "store_fixture"
FIXTURE_NAMESPACE = "namespace_fixture"
FIXTURE_DEPLOYMENT_IDENTITY_SHA256 = "4" * 64
MAX_ENVELOPE_BYTES = 256 * 1024
MAX_OPAQUE_ID_LENGTH = 128
MAX_ENUM_LENGTH = 64

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_RFC3339_UTC_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d+))?Z$"
)
_ACTION_TYPES = frozenset(action_type.value for action_type in ActionType)


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


def _require_sha256(label: str, value: object) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _require_commit(label: str, value: object) -> str:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ValueError(f"{label} must be a full lowercase commit hash")
    return value


def _require_opaque_id(
    label: str,
    value: object,
    *,
    max_length: int = MAX_OPAQUE_ID_LENGTH,
) -> str:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or not _OPAQUE_ID_RE.fullmatch(value)
    ):
        raise ValueError(f"{label} must be a bounded opaque identifier")
    return value


def _require_exact_ref(label: str, value: object, prefix: str) -> str:
    expected = re.compile(rf"^{re.escape(prefix)}[0-9a-f]{{64}}$")
    if not isinstance(value, str) or not expected.fullmatch(value):
        raise ValueError(f"{label} must be an opaque {prefix} reference")
    return value


def _require_positive_int(label: str, value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_nonnegative_int(label: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _require_exact_type(label: str, value: object, expected: type) -> None:
    if type(value) is not expected:
        raise TypeError(f"{label} must be exactly {expected.__name__}")


def _parse_rfc3339_utc(
    label: str,
    value: object,
) -> tuple[datetime, str]:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an RFC3339 UTC timestamp")
    match = _RFC3339_UTC_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        base = datetime.strptime(
            match.group("base"),
            "%Y-%m-%dT%H:%M:%S",
        )
    except ValueError as exc:
        raise ValueError(f"{label} must be an RFC3339 UTC timestamp") from exc
    return base, match.group("fraction") or ""


def _timestamp_lte(
    left: tuple[datetime, str],
    right: tuple[datetime, str],
) -> bool:
    if left[0] != right[0]:
        return left[0] < right[0]
    width = max(len(left[1]), len(right[1]))
    return left[1].ljust(width, "0") <= right[1].ljust(width, "0")


def _require_unique(label: str, values: tuple[str, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{label} must not contain duplicates")


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

    def __post_init__(self) -> None:
        started = _parse_rfc3339_utc(
            "evaluation_started_at",
            self.evaluation_started_at,
        )
        completed = _parse_rfc3339_utc(
            "evaluation_completed_at",
            self.evaluation_completed_at,
        )
        captured = _parse_rfc3339_utc("captured_at", self.captured_at)
        if not (
            _timestamp_lte(started, completed)
            and _timestamp_lte(completed, captured)
        ):
            raise ValueError(
                "timestamps must be ordered start <= completion <= capture"
            )


@dataclass(frozen=True)
class EvidenceContentHashes:
    source_sha256: str
    fixture_sha256: str
    projection_sha256: str
    receipt_payload_sha256: str
    payload_sha256: str
    envelope_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "source_sha256",
            "fixture_sha256",
            "projection_sha256",
            "receipt_payload_sha256",
            "payload_sha256",
            "envelope_sha256",
        ):
            _require_sha256(field_name, getattr(self, field_name))


@dataclass(frozen=True)
class EvidencePayloadContentHashes:
    source_sha256: str
    fixture_sha256: str
    projection_sha256: str
    receipt_payload_sha256: str
    payload_sha256: str

    def __post_init__(self) -> None:
        for field_name in (
            "source_sha256",
            "fixture_sha256",
            "projection_sha256",
            "receipt_payload_sha256",
            "payload_sha256",
        ):
            _require_sha256(field_name, getattr(self, field_name))


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

    def __post_init__(self) -> None:
        if self.attestation_schema != ZERO_EFFECT_ATTESTATION_SCHEMA:
            raise ValueError("unsupported zero-effect attestation schema")
        for field_name in (
            "verified_source_sha256",
            "verified_report_sha256",
            "verified_result_digest",
            "test_manifest_sha256",
        ):
            _require_sha256(field_name, getattr(self, field_name))
        _require_nonnegative_int(
            "isolation_tests_passed",
            self.isolation_tests_passed,
        )
        _require_opaque_id("verifier_id", self.verifier_id)
        _require_opaque_id(
            "verifier_version",
            self.verifier_version,
            max_length=MAX_ENUM_LENGTH,
        )
        _require_opaque_id("verification_run_id", self.verification_run_id)
        _require_sha256("signature", self.signature)


@dataclass(frozen=True)
class FixtureTrustAnchor:
    verifier_id: str
    verifier_version: str
    signature: str

    def __post_init__(self) -> None:
        _require_opaque_id("verifier_id", self.verifier_id)
        _require_opaque_id(
            "verifier_version",
            self.verifier_version,
            max_length=MAX_ENUM_LENGTH,
        )
        _require_sha256("signature", self.signature)


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

    def __post_init__(self) -> None:
        if self.environment != FIXTURE_DESTINATION_ENVIRONMENT:
            raise ValueError("destination environment must be local_fixture")
        _require_opaque_id("project_or_store", self.project_or_store)
        if self.project_or_store != FIXTURE_PROJECT_OR_STORE:
            raise ValueError("destination project/store is not the Gate 1 fixture")
        _require_opaque_id("namespace", self.namespace)
        if self.namespace != FIXTURE_NAMESPACE:
            raise ValueError("destination namespace is not the Gate 1 fixture")
        _require_sha256(
            "deployment_identity_sha256",
            self.deployment_identity_sha256,
        )
        if (
            self.deployment_identity_sha256
            != FIXTURE_DEPLOYMENT_IDENTITY_SHA256
        ):
            raise ValueError(
                "destination deployment identity is not the Gate 1 fixture"
            )


@dataclass(frozen=True)
class EvidenceProvenance:
    code_revision: str
    evidence_commit: str
    report_sha256: str
    result_digest: str
    fixture_schema: str
    source_marker: str
    fixture_ref: str

    def __post_init__(self) -> None:
        _require_commit("code_revision", self.code_revision)
        _require_commit("evidence_commit", self.evidence_commit)
        _require_sha256("report_sha256", self.report_sha256)
        _require_sha256("result_digest", self.result_digest)
        if self.fixture_schema != FIXTURE_SCHEMA:
            raise ValueError("unsupported fixture schema")
        if self.source_marker not in {"fixture", "local_fixture"}:
            raise ValueError("source_marker must identify fixture input")
        _require_opaque_id("fixture_ref", self.fixture_ref)


@dataclass(frozen=True)
class EvidenceSummary(_JsonContract):
    claim_count: int
    action_count: int
    warning_count: int

    def __post_init__(self) -> None:
        _require_nonnegative_int("claim_count", self.claim_count)
        _require_nonnegative_int("action_count", self.action_count)
        _require_nonnegative_int("warning_count", self.warning_count)


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
        _require_exact_ref("claim row_id", self.row_id, "row_")
        _require_positive_int("claim sequence", self.sequence)
        _require_exact_ref("claim_ref", self.claim_ref, "claim_ref_")
        if self.execution_status != "not_applicable_claim":
            raise ValueError("claim rows must use not_applicable_claim execution status")
        if self.disposition is not EvidenceDisposition.PROPOSED:
            raise ValueError("claim rows must use proposed disposition")
        if self.source_category != "fixture":
            raise ValueError("claim rows must use fixture source category")

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
        _require_exact_ref("action row_id", self.row_id, "row_")
        _require_positive_int("action sequence", self.sequence)
        _require_exact_ref("action_ref", self.action_ref, "action_ref_")
        if self.action_type not in _ACTION_TYPES:
            raise ValueError("unknown action type")
        object.__setattr__(self, "claim_refs", tuple(self.claim_refs))
        object.__setattr__(self, "dependency_refs", tuple(self.dependency_refs))
        for claim_ref in self.claim_refs:
            _require_exact_ref("claim_ref", claim_ref, "claim_ref_")
        for dependency_ref in self.dependency_refs:
            _require_exact_ref(
                "dependency_ref",
                dependency_ref,
                "action_ref_",
            )
        _require_unique("claim_refs", self.claim_refs)
        _require_unique("dependency_refs", self.dependency_refs)
        if self.action_ref in self.dependency_refs:
            raise ValueError("action row cannot depend on itself")
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
        if self.source_category != "fixture":
            raise ValueError("action rows must use fixture source category")

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


def _row_sort_key(
    row: EvidenceClaimRow | EvidenceActionRow,
) -> tuple[int, int, str]:
    row_kind = 0 if type(row) is EvidenceClaimRow else 1
    return row_kind, row.sequence, row.row_id


def _validate_rows(
    rows: tuple[EvidenceClaimRow | EvidenceActionRow, ...],
) -> None:
    if not all(
        type(row) in {EvidenceClaimRow, EvidenceActionRow}
        for row in rows
    ):
        raise TypeError("evidence rows must be evidence row values")
    if tuple(sorted(rows, key=_row_sort_key)) != rows:
        raise ValueError("evidence rows must use canonical order")

    row_ids = tuple(row.row_id for row in rows)
    _require_unique("row IDs", row_ids)
    claim_refs = tuple(
        row.claim_ref for row in rows if type(row) is EvidenceClaimRow
    )
    action_refs = tuple(
        row.action_ref for row in rows if type(row) is EvidenceActionRow
    )
    _require_unique("claim row references", claim_refs)
    _require_unique("action row references", action_refs)
    known_claim_refs = set(claim_refs)
    known_action_refs = set(action_refs)
    for row in rows:
        if type(row) is not EvidenceActionRow:
            continue
        if not set(row.claim_refs).issubset(known_claim_refs):
            raise ValueError("action row contains a foreign claim reference")
        if not set(row.dependency_refs).issubset(known_action_refs):
            raise ValueError("action row contains a foreign dependency reference")
@dataclass(frozen=True)
class EvidenceProjection(_JsonContract):
    summary: EvidenceSummary
    rows: tuple[EvidenceClaimRow | EvidenceActionRow, ...]

    def __post_init__(self) -> None:
        _require_exact_type("summary", self.summary, EvidenceSummary)
        object.__setattr__(self, "rows", tuple(self.rows))
        if len(self.rows) > 400:
            raise ValueError("evidence projection rows exceed 400 row limit")
        _validate_rows(self.rows)
        expected_summary = EvidenceSummary(
            claim_count=sum(type(row) is EvidenceClaimRow for row in self.rows),
            action_count=sum(type(row) is EvidenceActionRow for row in self.rows),
            warning_count=0,
        )
        if self.summary != expected_summary:
            raise ValueError("evidence projection summary must match rows")


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


def _validate_plan_receipt(
    plan: ActionPlan,
    receipt: DryRunCommitReceipt,
) -> tuple[dict[str, PlannedAction], dict[str, DryRunEffectReceipt]]:
    _require_exact_type("plan", plan, ActionPlan)
    _require_exact_type("receipt", receipt, DryRunCommitReceipt)
    if not all(type(action) is PlannedAction for action in plan.actions):
        raise TypeError("plan actions must be exact PlannedAction values")
    if not all(type(effect) is DryRunEffectReceipt for effect in receipt.effects):
        raise TypeError(
            "receipt effects must be exact DryRunEffectReceipt values"
        )
    for field_name in (
        "tenant_id",
        "plan_id",
        "decision_id",
        "contract_id",
        "contract_version",
        "snapshot_hash",
    ):
        if getattr(receipt, field_name, None) != getattr(plan, field_name, None):
            raise ValueError(
                f"receipt {field_name} must match plan {field_name}"
            )

    actions_by_id = {action.action_id: action for action in plan.actions}
    if None in actions_by_id:
        raise ValueError("plan actions must expose action_id")
    if len(actions_by_id) != len(plan.actions):
        raise ValueError("plan actions must not contain duplicate action_id values")
    effects_by_action = {
        effect.action_id: effect
        for effect in receipt.effects
    }
    if len(effects_by_action) != len(receipt.effects):
        raise ValueError("receipt effects must not contain duplicate action IDs")
    if set(effects_by_action) != set(actions_by_id):
        raise ValueError("receipt effects must match plan actions exactly")
    for action_id, action in actions_by_id.items():
        effect = effects_by_action[action_id]
        if (
            effect.plan_id != plan.plan_id
            or effect.idempotency_key != action.idempotency_key
            or effect.action_type != action.action_type.value
            or effect.sequence != action.sequence
        ):
            raise ValueError("receipt effect identity must match plan action")
        if any(
            dependency_action_id not in effects_by_action
            for dependency_action_id in action.dependencies
        ):
            raise ValueError("plan action contains a foreign dependency")
        expected_dependency_receipt_ids = tuple(
            effects_by_action[dependency_action_id].receipt_id
            for dependency_action_id in action.dependencies
        )
        if (
            effect.dependency_receipt_ids
            != expected_dependency_receipt_ids
        ):
            raise ValueError(
                "receipt effect dependencies must match plan dependencies"
            )
    return actions_by_id, effects_by_action


def _projection_from_bound_plan_receipt(
    plan: ActionPlan,
    effects_by_action: dict[str, DryRunEffectReceipt],
) -> EvidenceProjection:
    referenced_claim_ids = tuple(
        dict.fromkeys(
            claim_id
            for action in plan.actions
            for claim_id in action.source_claim_ids
        )
    )
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
        for action in sorted(
            plan.actions,
            key=lambda item: (item.sequence, item.action_id),
        )
    )
    return EvidenceProjection(
        summary=EvidenceSummary(
            claim_count=len(claim_rows),
            action_count=len(action_rows),
            warning_count=0,
        ),
        rows=claim_rows + action_rows,
    )


def project_disabled_evidence(
    *,
    plan: ActionPlan,
    claims: tuple[Claim, ...],
    receipt: DryRunCommitReceipt,
) -> EvidenceProjection:
    _, effects_by_action = _validate_plan_receipt(plan, receipt)
    if type(claims) is not tuple or not all(
        type(claim) is Claim for claim in claims
    ):
        raise TypeError("claims must be a tuple of exact Claim values")
    claims_by_id = {claim.claim_id: claim for claim in claims}
    if len(claims_by_id) != len(claims):
        raise ValueError("claims must not contain duplicate claim_id values")
    referenced_claim_ids = {
        claim_id
        for action in plan.actions
        for claim_id in action.source_claim_ids
    }
    if not referenced_claim_ids.issubset(claims_by_id):
        raise ValueError("plan references claims not supplied to projector")
    return _projection_from_bound_plan_receipt(plan, effects_by_action)


def _provenance_json(
    provenance: EvidenceProvenance,
) -> dict[str, object]:
    return {
        "code_revision": provenance.code_revision,
        "evidence_commit": provenance.evidence_commit,
        "report_sha256": provenance.report_sha256,
        "result_digest": provenance.result_digest,
        "fixture_schema": provenance.fixture_schema,
        "source_marker": provenance.source_marker,
        "fixture_ref": provenance.fixture_ref,
    }


def _serialized_contract(
    *,
    schema_version: str,
    run_id: str,
    taxonomy_version: str,
    provenance: EvidenceProvenance,
    adapter_mode: str,
    environment_marker: str,
    timestamps: EvidenceTimestamps,
    content_hashes: object,
    zero_effect_attestation: ZeroEffectAttestation | None,
    summary: EvidenceSummary,
    rows: tuple[EvidenceClaimRow | EvidenceActionRow, ...],
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "run_id": run_id,
        "taxonomy_version": taxonomy_version,
        **_provenance_json(provenance),
        "adapter_mode": adapter_mode,
        "environment_marker": environment_marker,
        "timestamps": _json_ready(timestamps),
        "content_hashes": _json_ready(content_hashes),
        "zero_effect_attestation": _json_ready(zero_effect_attestation),
        "summary": summary.to_dict(),
        "rows": [row.to_dict() for row in rows],
    }


@dataclass(frozen=True)
class DisabledEvidencePayload(_JsonContract):
    run_id: str
    taxonomy_version: str
    provenance: EvidenceProvenance
    adapter_mode: str
    environment_marker: str
    timestamps: EvidenceTimestamps
    content_hashes: EvidencePayloadContentHashes
    zero_effect_attestation: ZeroEffectAttestation
    summary: EvidenceSummary
    rows: tuple[EvidenceClaimRow | EvidenceActionRow, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported evidence payload schema")
        if self.taxonomy_version != TAXONOMY_VERSION:
            raise ValueError("unsupported evidence payload taxonomy")
        _require_exact_ref("run_id", self.run_id, "run_")
        if self.adapter_mode != "disabled":
            raise ValueError("adapter_mode must be exactly disabled")
        if self.environment_marker != FIXTURE_DESTINATION_ENVIRONMENT:
            raise ValueError("environment_marker must be local_fixture")
        _require_exact_type(
            "provenance",
            self.provenance,
            EvidenceProvenance,
        )
        _require_exact_type(
            "timestamps",
            self.timestamps,
            EvidenceTimestamps,
        )
        _require_exact_type(
            "content_hashes",
            self.content_hashes,
            EvidencePayloadContentHashes,
        )
        _require_exact_type(
            "zero_effect_attestation",
            self.zero_effect_attestation,
            ZeroEffectAttestation,
        )
        _require_exact_type("summary", self.summary, EvidenceSummary)
        object.__setattr__(self, "rows", tuple(self.rows))
        if len(self.rows) > 400:
            raise ValueError("evidence payload rows exceed 400 row limit")
        _validate_rows(self.rows)
        expected_summary = EvidenceSummary(
            claim_count=sum(type(row) is EvidenceClaimRow for row in self.rows),
            action_count=sum(type(row) is EvidenceActionRow for row in self.rows),
            warning_count=0,
        )
        if self.summary != expected_summary:
            raise ValueError("evidence payload summary must match rows")

    def to_dict(self) -> dict[str, object]:
        return _serialized_contract(
            schema_version=self.schema_version,
            run_id=self.run_id,
            taxonomy_version=self.taxonomy_version,
            provenance=self.provenance,
            adapter_mode=self.adapter_mode,
            environment_marker=self.environment_marker,
            timestamps=self.timestamps,
            content_hashes=self.content_hashes,
            zero_effect_attestation=self.zero_effect_attestation,
            summary=self.summary,
            rows=self.rows,
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
        _require_exact_ref("run_id", self.run_id, "run_")
        if self.adapter_mode != "disabled":
            raise ValueError("adapter_mode must be exactly disabled")
        if self.environment_marker != FIXTURE_DESTINATION_ENVIRONMENT:
            raise ValueError("environment_marker must be local_fixture")
        _require_exact_type(
            "provenance",
            self.provenance,
            EvidenceProvenance,
        )
        _require_exact_type(
            "timestamps",
            self.timestamps,
            EvidenceTimestamps,
        )
        _require_exact_type(
            "content_hashes",
            self.content_hashes,
            EvidenceContentHashes,
        )
        _require_exact_type(
            "destination_attestation",
            self.destination_attestation,
            EvidenceDestinationAttestation,
        )
        if self.destination_attestation.environment != self.environment_marker:
            raise ValueError(
                "destination environment must match environment_marker"
            )
        if self.zero_effect_attestation is not None:
            _require_exact_type(
                "zero_effect_attestation",
                self.zero_effect_attestation,
                ZeroEffectAttestation,
            )
        _require_exact_type("summary", self.summary, EvidenceSummary)
        object.__setattr__(self, "rows", tuple(self.rows))
        if len(self.rows) > 400:
            raise ValueError("evidence envelope rows exceed 400 row limit")
        _validate_rows(self.rows)
        expected_summary = EvidenceSummary(
            claim_count=sum(type(row) is EvidenceClaimRow for row in self.rows),
            action_count=sum(type(row) is EvidenceActionRow for row in self.rows),
            warning_count=0,
        )
        if self.summary != expected_summary:
            raise ValueError("evidence envelope summary must match rows")
        if len(canonical_json(self.to_dict())) > MAX_ENVELOPE_BYTES:
            raise ValueError("evidence envelope exceeds 256 KiB canonical limit")

    def to_dict(self) -> dict[str, object]:
        value = _serialized_contract(
            schema_version=self.schema_version,
            run_id=self.run_id,
            taxonomy_version=self.taxonomy_version,
            provenance=self.provenance,
            adapter_mode=self.adapter_mode,
            environment_marker=self.environment_marker,
            timestamps=self.timestamps,
            content_hashes=self.content_hashes,
            zero_effect_attestation=self.zero_effect_attestation,
            summary=self.summary,
            rows=self.rows,
        )
        value["destination_attestation"] = _json_ready(
            self.destination_attestation
        )
        return value


def _payload_basis_values(
    *,
    schema_version: str,
    run_id: str,
    taxonomy_version: str,
    provenance: EvidenceProvenance,
    timestamps: EvidenceTimestamps,
    projection_sha256: str,
    receipt_payload_sha256: str,
    zero_effect_attestation: ZeroEffectAttestation | None,
    summary: EvidenceSummary,
    rows: tuple[EvidenceClaimRow | EvidenceActionRow, ...],
) -> dict[str, object]:
    return _serialized_contract(
        schema_version=schema_version,
        run_id=run_id,
        taxonomy_version=taxonomy_version,
        provenance=provenance,
        adapter_mode="disabled",
        environment_marker=FIXTURE_DESTINATION_ENVIRONMENT,
        timestamps=timestamps,
        content_hashes={
            "source_sha256": SOURCE_SHA256,
            "fixture_sha256": FIXTURE_SHA256,
            "projection_sha256": projection_sha256,
            "receipt_payload_sha256": receipt_payload_sha256,
        },
        zero_effect_attestation=zero_effect_attestation,
        summary=summary,
        rows=rows,
    )


def serialize_disabled_evidence(
    *,
    plan: ActionPlan,
    receipt: DryRunCommitReceipt,
    projection: EvidenceProjection,
    provenance: EvidenceProvenance,
    timestamps: EvidenceTimestamps,
    zero_effect_attestation: ZeroEffectAttestation,
) -> DisabledEvidencePayload:
    _require_exact_type("projection", projection, EvidenceProjection)
    _require_exact_type("provenance", provenance, EvidenceProvenance)
    _require_exact_type("timestamps", timestamps, EvidenceTimestamps)
    _require_exact_type(
        "zero_effect_attestation",
        zero_effect_attestation,
        ZeroEffectAttestation,
    )
    _, effects_by_action = _validate_plan_receipt(plan, receipt)
    expected_projection = _projection_from_bound_plan_receipt(
        plan,
        effects_by_action,
    )
    if projection != expected_projection:
        raise ValueError(
            "projection must exactly match its plan and dry-run receipt"
        )
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
    payload_basis = _payload_basis_values(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        taxonomy_version=TAXONOMY_VERSION,
        provenance=provenance,
        timestamps=timestamps,
        projection_sha256=projection_sha256,
        receipt_payload_sha256=receipt_payload_sha256,
        zero_effect_attestation=zero_effect_attestation,
        summary=projection.summary,
        rows=projection.rows,
    )
    payload_sha256 = hashlib.sha256(canonical_json(payload_basis)).hexdigest()
    hashes = EvidencePayloadContentHashes(
        source_sha256=SOURCE_SHA256,
        fixture_sha256=FIXTURE_SHA256,
        projection_sha256=projection_sha256,
        receipt_payload_sha256=receipt_payload_sha256,
        payload_sha256=payload_sha256,
    )
    return DisabledEvidencePayload(
        run_id=run_id,
        taxonomy_version=TAXONOMY_VERSION,
        provenance=provenance,
        adapter_mode="disabled",
        environment_marker=FIXTURE_DESTINATION_ENVIRONMENT,
        timestamps=timestamps,
        content_hashes=hashes,
        zero_effect_attestation=zero_effect_attestation,
        summary=projection.summary,
        rows=projection.rows,
    )


def bind_fixture_evidence_envelope(
    payload: DisabledEvidencePayload,
    *,
    plan: ActionPlan,
    receipt: DryRunCommitReceipt,
    trust_anchor: FixtureTrustAnchor,
) -> DisabledEvidenceEnvelope:
    _require_exact_type("payload", payload, DisabledEvidencePayload)
    _require_exact_type("trust_anchor", trust_anchor, FixtureTrustAnchor)
    if (
        payload.schema_version != SCHEMA_VERSION
        or payload.taxonomy_version != TAXONOMY_VERSION
    ):
        raise ValueError("payload schema or taxonomy is not approved")
    _, effects_by_action = _validate_plan_receipt(plan, receipt)
    expected_projection = _projection_from_bound_plan_receipt(
        plan,
        effects_by_action,
    )
    if (
        payload.summary != expected_projection.summary
        or payload.rows != expected_projection.rows
    ):
        raise ValueError("payload projection is not bound to the supplied plan")
    expected_receipt_sha256 = _receipt_payload_sha256(receipt)
    if (
        payload.content_hashes.receipt_payload_sha256
        != expected_receipt_sha256
    ):
        raise ValueError("payload receipt hash does not match supplied receipt")
    expected_projection_sha256 = _projection_sha256(
        payload.summary,
        payload.rows,
    )
    if (
        payload.content_hashes.source_sha256 != SOURCE_SHA256
        or payload.content_hashes.fixture_sha256 != FIXTURE_SHA256
        or payload.content_hashes.projection_sha256
        != expected_projection_sha256
    ):
        raise ValueError("payload source or projection integrity mismatch")
    expected_run_id = derive_run_id(
        receipt_id=receipt.receipt_id,
        projection_sha256=expected_projection_sha256,
        fixture_sha256=FIXTURE_SHA256,
        code_revision=payload.provenance.code_revision,
        result_digest=payload.provenance.result_digest,
    )
    if payload.run_id != expected_run_id:
        raise ValueError("payload run identity does not match supplied receipt")
    if not _provenance_matches_gate_1_baseline(payload.provenance):
        raise ValueError("payload provenance is not the approved Gate 1 baseline")
    if not _attestation_matches_trust_anchor(
        payload.zero_effect_attestation,
        trust_anchor,
    ):
        raise ValueError("payload zero-effect attestation is not trusted")
    payload_basis = _payload_basis_values(
        schema_version=payload.schema_version,
        run_id=payload.run_id,
        taxonomy_version=payload.taxonomy_version,
        provenance=payload.provenance,
        timestamps=payload.timestamps,
        projection_sha256=payload.content_hashes.projection_sha256,
        receipt_payload_sha256=payload.content_hashes.receipt_payload_sha256,
        zero_effect_attestation=payload.zero_effect_attestation,
        summary=payload.summary,
        rows=payload.rows,
    )
    expected_payload_sha256 = hashlib.sha256(
        canonical_json(payload_basis)
    ).hexdigest()
    if expected_payload_sha256 != payload.content_hashes.payload_sha256:
        raise ValueError("payload hash integrity mismatch")

    destination_attestation = EvidenceDestinationAttestation(
        environment=FIXTURE_DESTINATION_ENVIRONMENT,
        project_or_store=FIXTURE_PROJECT_OR_STORE,
        namespace=FIXTURE_NAMESPACE,
        deployment_identity_sha256=FIXTURE_DEPLOYMENT_IDENTITY_SHA256,
    )
    envelope_basis = payload.to_dict()
    envelope_basis["destination_attestation"] = _json_ready(
        destination_attestation
    )
    envelope_sha256 = hashlib.sha256(
        canonical_json(envelope_basis)
    ).hexdigest()
    hashes = EvidenceContentHashes(
        source_sha256=payload.content_hashes.source_sha256,
        fixture_sha256=payload.content_hashes.fixture_sha256,
        projection_sha256=payload.content_hashes.projection_sha256,
        receipt_payload_sha256=payload.content_hashes.receipt_payload_sha256,
        payload_sha256=payload.content_hashes.payload_sha256,
        envelope_sha256=envelope_sha256,
    )
    return DisabledEvidenceEnvelope(
        run_id=payload.run_id,
        taxonomy_version=payload.taxonomy_version,
        provenance=payload.provenance,
        adapter_mode=payload.adapter_mode,
        environment_marker=payload.environment_marker,
        timestamps=payload.timestamps,
        content_hashes=hashes,
        zero_effect_attestation=payload.zero_effect_attestation,
        destination_attestation=destination_attestation,
        summary=payload.summary,
        rows=payload.rows,
        schema_version=payload.schema_version,
    )


def _projection_sha256(summary: EvidenceSummary, rows: tuple[object, ...]) -> str:
    projection_dict = {
        "summary": summary.to_dict(),
        "rows": [row.to_dict() for row in rows],
    }
    return hashlib.sha256(canonical_json(projection_dict)).hexdigest()


def _receipt_payload_sha256(receipt: DryRunCommitReceipt) -> str:
    _require_exact_type("receipt", receipt, DryRunCommitReceipt)
    return hashlib.sha256(canonical_json(receipt.to_dict())).hexdigest()


def _payload_basis(
    envelope: DisabledEvidenceEnvelope,
    *,
    projection_sha256: str,
    receipt_payload_sha256: str,
) -> dict[str, object]:
    return _payload_basis_values(
        schema_version=envelope.schema_version,
        run_id=envelope.run_id,
        taxonomy_version=envelope.taxonomy_version,
        provenance=envelope.provenance,
        timestamps=envelope.timestamps,
        projection_sha256=projection_sha256,
        receipt_payload_sha256=receipt_payload_sha256,
        zero_effect_attestation=envelope.zero_effect_attestation,
        summary=envelope.summary,
        rows=envelope.rows,
    )


def _computed_hashes_from_receipt_hash(
    envelope: DisabledEvidenceEnvelope,
    *,
    receipt_payload_sha256: str,
) -> EvidenceContentHashes:
    projection_hash = _projection_sha256(envelope.summary, envelope.rows)
    payload_basis = _payload_basis(
        envelope,
        projection_sha256=projection_hash,
        receipt_payload_sha256=receipt_payload_sha256,
    )
    payload_sha256 = hashlib.sha256(canonical_json(payload_basis)).hexdigest()
    envelope_basis = dict(payload_basis)
    envelope_basis["destination_attestation"] = _json_ready(
        envelope.destination_attestation
    )
    envelope_basis["content_hashes"] = {
        **payload_basis["content_hashes"],
        "payload_sha256": payload_sha256,
    }
    envelope_sha256 = hashlib.sha256(canonical_json(envelope_basis)).hexdigest()
    return EvidenceContentHashes(
        source_sha256=SOURCE_SHA256,
        fixture_sha256=FIXTURE_SHA256,
        projection_sha256=projection_hash,
        receipt_payload_sha256=receipt_payload_sha256,
        payload_sha256=payload_sha256,
        envelope_sha256=envelope_sha256,
    )


def _computed_hashes(
    envelope: DisabledEvidenceEnvelope,
    *,
    receipt: DryRunCommitReceipt,
) -> EvidenceContentHashes:
    return _computed_hashes_from_receipt_hash(
        envelope,
        receipt_payload_sha256=_receipt_payload_sha256(receipt),
    )


def _envelope_integrity_matches(
    envelope: DisabledEvidenceEnvelope,
    *,
    receipt: DryRunCommitReceipt,
) -> bool:
    if envelope.schema_version != SCHEMA_VERSION:
        return False
    if envelope.taxonomy_version != TAXONOMY_VERSION:
        return False
    if envelope.adapter_mode != "disabled":
        return False
    if envelope.environment_marker != "local_fixture":
        return False
    expected_hashes = _computed_hashes(envelope, receipt=receipt)
    expected_run_id = derive_run_id(
        receipt_id=receipt.receipt_id,
        projection_sha256=expected_hashes.projection_sha256,
        fixture_sha256=expected_hashes.fixture_sha256,
        code_revision=envelope.provenance.code_revision,
        result_digest=envelope.provenance.result_digest,
    )
    return (
        envelope.run_id == expected_run_id
        and envelope.content_hashes == expected_hashes
    )


def _fixture_attestation_signature(
    attestation: ZeroEffectAttestation,
) -> str:
    _require_exact_type(
        "zero_effect_attestation",
        attestation,
        ZeroEffectAttestation,
    )
    unsigned = _json_ready(attestation)
    if not isinstance(unsigned, dict):
        raise TypeError("attestation must serialize to a JSON object")
    unsigned.pop("signature")
    return hashlib.sha256(
        b"sitesift-zero-effect-attestation-fixture-v1\0"
        + canonical_json(unsigned)
    ).hexdigest()


def _attestation_matches_trust_anchor(
    attestation: ZeroEffectAttestation,
    trust_anchor: FixtureTrustAnchor,
) -> bool:
    return (
        trust_anchor.verifier_id == TRUSTED_FIXTURE_VERIFIER_ID
        and trust_anchor.verifier_version
        == TRUSTED_FIXTURE_VERIFIER_VERSION
        and trust_anchor.signature
        == TRUSTED_FIXTURE_ATTESTATION_SIGNATURE
        and attestation.attestation_schema == ZERO_EFFECT_ATTESTATION_SCHEMA
        and attestation.verifier_id == TRUSTED_FIXTURE_VERIFIER_ID
        and attestation.verifier_version
        == TRUSTED_FIXTURE_VERIFIER_VERSION
        and attestation.signature
        == TRUSTED_FIXTURE_ATTESTATION_SIGNATURE
        and attestation.signature
        == _fixture_attestation_signature(attestation)
    )


def _provenance_matches_gate_1_baseline(
    provenance: EvidenceProvenance,
) -> bool:
    return (
        provenance.code_revision == CODE_REVISION
        and provenance.evidence_commit == EVIDENCE_COMMIT
        and provenance.report_sha256 == REPORT_SHA256
        and provenance.result_digest == RESULT_DIGEST
        and provenance.fixture_schema == FIXTURE_SCHEMA
        and provenance.source_marker in {"fixture", "local_fixture"}
    )


def verify_disabled_evidence_envelope(
    envelope: object,
    *,
    receipt: DryRunCommitReceipt,
    trust_anchor: FixtureTrustAnchor,
) -> EvidenceVerificationResult:
    if type(envelope) is not DisabledEvidenceEnvelope:
        raise TypeError("verifier requires a DisabledEvidenceEnvelope")
    if envelope.schema_version != SCHEMA_VERSION:
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="unsupported_schema",
            warning_disposition=EvidenceDisposition.UNKNOWN_TAXONOMY,
        )
    if envelope.taxonomy_version != TAXONOMY_VERSION:
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="unsupported_taxonomy",
            warning_disposition=EvidenceDisposition.UNKNOWN_TAXONOMY,
        )
    _require_exact_type("receipt", receipt, DryRunCommitReceipt)
    _require_exact_type("trust_anchor", trust_anchor, FixtureTrustAnchor)
    attestation = envelope.zero_effect_attestation
    if type(attestation) is not ZeroEffectAttestation:
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="missing_zero_effect_attestation",
        )
    if not _attestation_matches_trust_anchor(attestation, trust_anchor):
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="invalid_zero_effect_attestation",
        )
    if not _provenance_matches_gate_1_baseline(envelope.provenance):
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="invalid_provenance",
            warning_disposition=EvidenceDisposition.INVALID_INPUT,
        )
    if not _envelope_integrity_matches(envelope, receipt=receipt):
        return EvidenceVerificationResult(
            verified=False,
            include_in_normal_reads=False,
            failure_code="hash_integrity_mismatch",
        )
    attestation_matches_envelope = (
        attestation.verified_source_sha256 == envelope.content_hashes.source_sha256
        and attestation.verified_report_sha256 == envelope.provenance.report_sha256
        and attestation.verified_result_digest == envelope.provenance.result_digest
    )
    if not attestation_matches_envelope:
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
    if (
        type(existing) is not DisabledEvidenceEnvelope
        or type(candidate) is not DisabledEvidenceEnvelope
    ):
        raise TypeError("duplicate classification requires evidence envelopes")
    if existing.run_id != candidate.run_id:
        return EvidenceDuplicateResult(outcome="new_run", should_write=True)
    existing_hash = _computed_hashes_from_receipt_hash(
        existing,
        receipt_payload_sha256=existing.content_hashes.receipt_payload_sha256,
    )
    candidate_hash = _computed_hashes_from_receipt_hash(
        candidate,
        receipt_payload_sha256=candidate.content_hashes.receipt_payload_sha256,
    )
    existing_valid = existing_hash == existing.content_hashes
    candidate_valid = candidate_hash == candidate.content_hashes
    if (
        existing_valid
        and candidate_valid
        and existing_hash.envelope_sha256 == candidate_hash.envelope_sha256
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
