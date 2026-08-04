"""Pure contracts for B1 exact-source coordination."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from uuid import uuid4

from google.cloud.firestore_v1 import transactional


SOURCE_COORDINATOR_MODE_ENV = "SITESIFT_SOURCE_COORDINATOR_MODE"
MAX_SOURCE_ALIAS_BYTES = 1024
MAX_SOURCE_ALIASES = 8
MAX_CLASSIFICATION_SNAPSHOT_BYTES = 614400
MAX_SOURCE_WORK_ENTRIES = 128
MAX_SOURCE_WORK_LEDGER_BYTES = 600 * 1024
MAX_SOURCE_WORK_TRANSACTION_WRITES = 400
MAX_BLOCKED_SOURCES_PER_THREAD = 100
MAX_UNSETTLED_SOURCE_ADMISSIONS = 500
SOURCE_AUTHORITY_AUDIT_PAGE_SIZE = 200
_SOURCE_AUTHORITY_REVISION_FIELD = "sourceAuthorityRevision"
_SOURCE_AUTHORITY_AUDIT_REVISION_FIELD = "sourceAuthorityAuditRevision"
_SOURCE_AUTHORITY_VERDICT_COLLECTIONS = {
    "sourceIdentities",
    "inboundPendingAdmissions",
    "threadTransitionHeads",
}
_SOURCE_AUTHORITY_SUPPORT_COLLECTIONS = (
    "sourceAliases",
    "sourceClassifications",
    "sourceTransitionOwners",
    "sourceWorkLedgers",
    "sourceDeferredWork",
    "blockedSources",
    "sourceSettlements",
)
_SOURCE_AUTHORITY_INVENTORY_COLLECTIONS = {
    *_SOURCE_AUTHORITY_VERDICT_COLLECTIONS,
    *_SOURCE_AUTHORITY_SUPPORT_COLLECTIONS,
}
_SOURCE_PROCESSED_OWNERSHIP_FIELDS = {
    "canonicalSourceId",
    "settlementRevision",
    "settlementHash",
    "sourceAliasKey",
}
_SOURCE_ALIAS_KEY_DOMAIN = "source-alias-v2"
_SOURCE_ALIAS_TYPES = {"graph", "internet_message_id"}
_SOURCE_ADMISSION_EVIDENCE_KINDS = {"graph_hydration", "operator_replay"}
_SOURCE_IDENTITY_SCHEMA_VERSION = 1
_SOURCE_ALIAS_VALUE_HASH_KIND = "source-alias-normalized-value-v1"
_FIRESTORE_DOCUMENT_ID_MAX_BYTES = 1500
_SOURCE_ALIAS_PROJECTION_FIELDS = {
    "schemaVersion",
    "sourceAliasKey",
    "aliasType",
    "normalizedValueHash",
    "canonicalSourceId",
    "createdAt",
}
_SOURCE_IDENTITY_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "creationHash",
    "verifiedAliases",
    "threadId",
    "lifecycleState",
    "createdAt",
    "updatedAt",
}
_CLASSIFICATION_SCHEMA_VERSION = 1
_CLASSIFICATION_INPUT_SCHEMA_VERSION = 1
_CLASSIFICATION_SNAPSHOT_SCHEMA_VERSION = 1
_COMPLETE_PROPOSAL_FIELDS = {
    "schemaVersion",
    "transitionCandidates",
    "ordinaryObligations",
}
_CLASSIFICATION_REQUEST_KEY_KIND = "source-model-request-v1"
_CLASSIFICATION_SNAPSHOT_HASH_KIND = "source-classification-snapshot-v1"
_CLASSIFICATION_SELECTION_HASH_KIND = "source-selection-v1"
_CANDIDATE_TAXONOMY_VERSION = "source-candidate-taxonomy-v1"
_RETAINED_TERMINAL_BINDING_HASH_KIND = "retained-terminal-binding-v1"
_RETAINED_TERMINAL_RECORD_HASH_KIND = "retained-terminal-record-v1"
_RETAINED_TERMINAL_CLASSIFICATION_FIELDS = {
    "retainedTerminalKind",
    "retainedTerminalImmutableHash",
    "retainedTerminalRecordHash",
    "retainedTerminalBindingHash",
}
_CLASSIFICATION_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "classificationState",
    "classificationEpoch",
    "classificationClaimId",
    "leaseExpiresAt",
    "classificationInputSchemaVersion",
    "classificationInputHash",
    "modelRequestKey",
    "modelRequestState",
    "requestStartFence",
    "completeProposalSnapshot",
    "completeProposalHash",
    "transitionCandidates",
    "ordinaryObligations",
    "selectionSnapshot",
    "selectionHash",
    "snapshotImmutableHash",
    "proposalEvidence",
    "proposalEvidenceHash",
    "deterministicEvidence",
    "deterministicEvidenceHash",
    "snapshotPersistedAt",
    *_RETAINED_TERMINAL_CLASSIFICATION_FIELDS,
    "createdAt",
    "updatedAt",
}
_CLASSIFICATION_SNAPSHOT_FIELDS = {
    "completeProposalSnapshot",
    "completeProposalHash",
    "transitionCandidates",
    "ordinaryObligations",
    "selectionSnapshot",
    "selectionHash",
    "snapshotImmutableHash",
    "proposalEvidence",
    "proposalEvidenceHash",
    "deterministicEvidence",
    "deterministicEvidenceHash",
    "snapshotPersistedAt",
}
_TERMINAL_CANDIDATE_TYPES = {"property_unavailable", "close_conversation"}
_HUMAN_CANDIDATE_TYPES = {
    "call_requested",
    "actionable_tour_review",
    "needs_user_input",
    "wrong_contact_pause",
    "forwarded_observed",
    "disabled_policy_suppressed",
}
_ORDINARY_CANDIDATE_TYPES = {
    "confirmed_tour",
    "non_tour",
    "new_property",
    "field_update",
    "generic_reply",
    "informational",
}
_MODEL_REQUEST_KEY_MAX_BYTES = 1024
_HARD_OPTOUT_EVIDENCE_FIELDS = {
    "schemaVersion",
    "evidenceKind",
    "evidenceHash",
}
_LOCAL_SOURCE_POLICY_EVIDENCE_KINDS = {
    "local_ignore_auto_reply",
    "local_ignore_self_sender",
}
_TRANSITION_OWNER_SCHEMA_VERSION = 1
_SOURCE_WORK_LEDGER_SCHEMA_VERSION = 1
_TRANSITION_OWNER_HASH_KIND = "source-transition-owner-v1"
_SOURCE_WORK_KEY_HASH_KIND = "source-work-key-v1"
_SOURCE_WORK_LEDGER_HASH_KIND = "source-work-ledger-v1"
_TRANSITION_OWNER_KINDS = {
    "none",
    "contact_optout",
    "terminal",
    "human_decision",
}
_TRANSITION_OWNER_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "snapshotImmutableHash",
    "selectionHash",
    "ownerKind",
    "ownerKey",
    "ownerDecisionHash",
    "revision",
    "createdAt",
    "updatedAt",
}
_SOURCE_WORK_LEDGER_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "completeProposalHash",
    "snapshotImmutableHash",
    "selectionHash",
    "ownerDecisionHash",
    "entries",
    "entryCount",
    "ledgerHash",
    "revision",
    "createdAt",
    "updatedAt",
}
_SOURCE_WORK_ENTRY_FIELDS = {
    "workKey",
    "lane",
    "kind",
    "payload",
    "payloadHash",
    "occurrenceOrdinal",
    "selectedOwnerKind",
    "selectedOwnerKey",
    "dominanceOutcome",
    "completionContract",
    "state",
    "resolutionEvidence",
    "resolutionEvidenceHash",
}
_SOURCE_WORK_ENTRY_MUTABLE_FIELDS = {
    "state",
    "resolutionEvidence",
    "resolutionEvidenceHash",
}
_SOURCE_WORK_ENTRY_IMMUTABLE_FIELDS = (
    _SOURCE_WORK_ENTRY_FIELDS - _SOURCE_WORK_ENTRY_MUTABLE_FIELDS
)
_SOURCE_WORK_RESOLUTION_EVIDENCE_HASH_KIND = (
    "source-work-resolution-evidence-v1"
)
_SOURCE_DEFERRED_WORK_SCHEMA_VERSION = 1
_SOURCE_DEFERRED_WORK_HASH_KIND = "source-deferred-work-v1"
_COMPLETION_RECORD_FIELDS = {
    "schemaVersion",
    "evidenceKind",
    "workKind",
    "resultHash",
}
_COMPLETION_EVIDENCE_FIELDS = {
    "schemaVersion",
    "evidenceKind",
    "canonicalSourceId",
    "ledgerHash",
    "workKey",
    "payloadHash",
    "workKind",
    "resultHash",
}
_DELEGATION_EVIDENCE_FIELDS = {
    "schemaVersion",
    "evidenceKind",
    "canonicalSourceId",
    "ledgerHash",
    "workKey",
    "payloadHash",
    "workKind",
    "deferredBindingHash",
}
_DOMINANCE_EVIDENCE_FIELDS = {
    "schemaVersion",
    "evidenceKind",
    "canonicalSourceId",
    "ledgerHash",
    "workKey",
    "payloadHash",
    "workKind",
    "selectionHash",
    "ownerDecisionHash",
    "dominatingOwnerKind",
    "dominatingOwnerKey",
    "dominanceOutcome",
}
_SOURCE_DEFERRED_WORK_FIELDS = {
    "schemaVersion",
    "workKey",
    "canonicalSourceId",
    "ledgerHash",
    "entryPayloadHash",
    "targetOwnerKind",
    "targetOwnerKey",
    "wakeCondition",
    "completionContract",
    "bindingHash",
    "state",
    "createdAt",
    "updatedAt",
}
_SOURCE_SETTLEMENT_SCHEMA_VERSION = 1
_PROCESSED_ALIAS_SCHEMA_VERSION = 1
_SOURCE_SETTLEMENT_IDENTITY_HASH_KIND = "source-settlement-identity-v1"
_SOURCE_SETTLEMENT_ALIAS_SET_HASH_KIND = "source-settlement-alias-set-v1"
_FINAL_LEDGER_EVIDENCE_HASH_KIND = "source-final-ledger-evidence-v1"
_SOURCE_SETTLEMENT_HASH_KIND = "source-settlement-v1"
_SOURCE_SETTLEMENT_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "identityHash",
    "snapshotImmutableHash",
    "selectionHash",
    "ownerDecisionHash",
    "ledgerHash",
    "finalLedgerEvidenceHash",
    "threadHeadBinding",
    "aliases",
    "aliasSetHash",
    "settlementRevision",
    "settlementHash",
    "settledAt",
}
_PROCESSED_ALIAS_FIELDS = {
    "schemaVersion",
    "sourceAliasKey",
    "aliasType",
    "normalizedValueHash",
    "canonicalSourceId",
    "settlementRevision",
    "settlementHash",
    "processedAt",
}
_THREAD_HEAD_SCHEMA_VERSION = 1
_PENDING_ADMISSION_SCHEMA_VERSION = 1
_BLOCKED_SOURCE_SCHEMA_VERSION = 1
_THREAD_HEAD_HASH_KIND = "thread-transition-head-v1"
_PENDING_ADMISSION_HASH_KIND = "inbound-pending-admission-v1"
_WAKE_TOKEN_HASH_KIND = "source-wake-token-v1"
_THREAD_HEAD_FIELDS = {
    "schemaVersion",
    "threadId",
    "threadHeadRevision",
    "activeOwnerKey",
    "activeOwnerKind",
    "activeCanonicalSourceId",
    "activeGeneration",
    "activeState",
    "headHash",
    "createdAt",
    "updatedAt",
}
_BLOCKER_FIELDS = {
    "canonicalSourceId",
    "ownerKind",
    "ownerKey",
    "generation",
    "threadHeadRevision",
    "headHash",
}
_PENDING_ADMISSION_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "threadId",
    "identityCreationHash",
    "snapshotImmutableHash",
    "selectionHash",
    "ownerDecisionHash",
    "ledgerHash",
    "ownerKind",
    "ownerKey",
    "savedHistoryBinding",
    "savedHistoryBindingHash",
    "indexBinding",
    "indexBindingHash",
    "receivedAt",
    "sentAt",
    "admissionHash",
    "admissionState",
    "blockedLifecycleState",
    "initialBlocker",
    "currentBlocker",
    "wakeGeneration",
    "wakeToken",
    "wakeState",
    "wakeClaimId",
    "revision",
    "createdAt",
    "updatedAt",
}
_SAVED_HISTORY_BINDING_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "threadId",
    "historyDocumentId",
    "historyHash",
}
_SOURCE_INDEX_BINDING_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "threadId",
    "identityDocumentId",
}
_BLOCKED_SOURCE_FIELDS = {
    "schemaVersion",
    "canonicalSourceId",
    "threadId",
    "admissionHash",
    "admissionRevision",
    "receivedAt",
    "sentAt",
    "blockedLifecycleState",
    "currentBlocker",
    "wakeGeneration",
    "wakeState",
    "createdAt",
    "updatedAt",
}


class CoordinatorMode(str, Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    ENFORCED = "enforced"


class SourceCoordinatorError(RuntimeError):
    code = "source_coordinator_error"


class SourceCoordinatorRetryable(SourceCoordinatorError):
    code = "source_coordinator_retryable"


class SourceCoordinatorAmbiguous(SourceCoordinatorError):
    code = "source_coordinator_ambiguous"


class SourceCoordinatorConflict(SourceCoordinatorError):
    code = "source_coordinator_conflict"


class SourceCoordinatorConfigError(SourceCoordinatorError):
    code = "source_coordinator_config"


class SourceIdentityMissing(SourceCoordinatorConflict):
    code = "source_identity_missing"


class SourceAliasConflict(SourceCoordinatorConflict):
    code = "source_alias_conflict"


class SourceAliasBridgeRequired(SourceCoordinatorConflict):
    code = "source_alias_bridge_required"


class SourceThreadConflict(SourceCoordinatorConflict):
    code = "source_thread_conflict"


class SourceAliasLimitExceeded(SourceCoordinatorConflict):
    code = "source_alias_limit_exceeded"


class ClassificationClaimUnavailable(SourceCoordinatorRetryable):
    code = "classification_claim_unavailable"


class ClassificationClaimConflict(SourceCoordinatorConflict):
    code = "classification_claim_conflict"


class ClassificationClaimExpired(SourceCoordinatorConflict):
    code = "classification_claim_expired"


class ClassificationInputConflict(SourceCoordinatorConflict):
    code = "classification_input_conflict"


class ClassificationRequestAmbiguous(SourceCoordinatorAmbiguous):
    code = "classification_request_ambiguous"


class ClassificationSnapshotNotReady(SourceCoordinatorRetryable):
    code = "classification_snapshot_not_ready"


class ClassificationSnapshotConflict(SourceCoordinatorConflict):
    code = "classification_snapshot_conflict"


class ClassificationSnapshotTooLarge(SourceCoordinatorConfigError):
    code = "classification_snapshot_too_large"


class RetainedTerminalAuthorityConflict(SourceCoordinatorConflict):
    code = "legacy_terminal_authority_conflict"


class TransitionOwnerConflict(SourceCoordinatorConflict):
    code = "source_transition_owner_conflict"


class SourceWorkLedgerConflict(SourceCoordinatorConflict):
    code = "source_work_ledger_conflict"


class SourceWorkLedgerLimitExceeded(SourceCoordinatorConfigError):
    code = "source_work_ledger_limit_exceeded"


class PendingAdmissionConflict(SourceCoordinatorConflict):
    code = "pending_admission_conflict"


class ThreadTransitionConflict(SourceCoordinatorConflict):
    code = "thread_transition_conflict"


class ThreadQueueLimitExceeded(SourceCoordinatorConflict):
    code = "thread_queue_limit_exceeded"


class WakeReleaseConflict(SourceCoordinatorConflict):
    code = "wake_release_conflict"


class WakeClaimConflict(SourceCoordinatorConflict):
    code = "wake_claim_conflict"


class SourceWorkTransitionConflict(SourceCoordinatorConflict):
    code = "source_work_transition_conflict"


class DeferredWorkConflict(SourceCoordinatorConflict):
    code = "source_deferred_work_conflict"


class SourceSettlementNotReady(SourceCoordinatorRetryable):
    code = "source_settlement_not_ready"


class SourceSettlementConflict(SourceCoordinatorConflict):
    code = "source_settlement_conflict"


@dataclass(frozen=True)
class SourceAlias:
    alias_type: str
    value: str
    key: str = ""


@dataclass(frozen=True)
class SourceIdentityResult:
    canonical_source_id: str
    aliases: Sequence[SourceAlias]
    created: bool
    repaired: bool


@dataclass(frozen=True)
class ClassificationClaim:
    canonical_source_id: str
    classification_epoch: int
    classification_claim_id: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class ClassificationRequestStart:
    canonical_source_id: str
    classification_epoch: int
    classification_claim_id: str
    model_request_key: str
    classification_input_hash: str
    newly_started: bool


@dataclass(frozen=True)
class ClassificationSnapshot:
    canonical_source_id: str
    complete_proposal: Mapping[str, Any]
    complete_proposal_hash: str
    selection_snapshot: Mapping[str, Any]
    selection_hash: str
    snapshot_immutable_hash: str


@dataclass(frozen=True)
class RetainedTerminalAuthorityDisposition:
    canonical_source_id: str
    state: str
    terminal_kind: str | None
    evidence_hash: str | None
    created: bool


@dataclass(frozen=True)
class PendingAdmissionResult:
    canonical_source_id: str
    thread_id: str
    admission_hash: str
    state: str
    created: bool


@dataclass(frozen=True)
class ThreadTransitionResult:
    canonical_source_id: str
    thread_id: str
    disposition: str
    generation: int
    head_revision: int
    blocker_canonical_source_id: str | None


@dataclass(frozen=True)
class WakeReleaseResult:
    thread_id: str
    released_canonical_source_id: str
    next_canonical_source_id: str | None
    wake_generation: int | None
    wake_token: str | None
    head_state: str


@dataclass(frozen=True)
class WakeClaimResult:
    thread_id: str
    canonical_source_id: str
    wake_generation: int
    wake_token: str
    wake_claim_id: str
    head_revision: int


@dataclass(frozen=True)
class SourceWorkTransitionResult:
    canonical_source_id: str
    work_key: str
    state: str
    ledger_hash: str
    ledger_revision: int
    evidence_hash: str | None
    newly_started: bool = False


@dataclass(frozen=True)
class DeferredWorkResult:
    canonical_source_id: str
    work_key: str
    ledger_hash: str
    binding_hash: str
    deferred_state: str
    ledger_state: str
    ledger_revision: int


@dataclass(frozen=True)
class SourceSettlementResult:
    canonical_source_id: str
    settlement_hash: str
    settlement_revision: int
    alias_projection_count: int
    repaired_projection_count: int


@dataclass(frozen=True)
class DurableSourceResumeContext:
    canonical_source_id: str
    thread_id: str
    resume_state: str
    received_at: datetime
    sent_at: datetime
    saved_history_binding: Mapping[str, Any]
    index_binding: Mapping[str, Any]
    source_alias_keys: tuple[str, ...]
    required_source_alias_key: str
    snapshot: ClassificationSnapshot
    owner: Mapping[str, Any]
    ledger: Mapping[str, Any]
    retained_history: Mapping[str, Any]


@dataclass(frozen=True)
class DurableSourceResumeResult:
    canonical_source_id: str
    thread_id: str
    state: str
    settlement: SourceSettlementResult | None


@dataclass(frozen=True)
class SettledSourceReleaseCandidate:
    canonical_source_id: str
    thread_id: str


@dataclass(frozen=True)
class _SourceAdmissionEnvelope:
    aliases: Sequence[SourceAlias]
    evidence_kind: str
    evidence_hash: str


@dataclass(frozen=True)
class _SourceIdentityTransactionPlan:
    result: SourceIdentityResult
    identity_ref: Any
    alias_refs: Sequence[Any]
    revision_ref: Any | None
    before_state: Mapping[str, Mapping[str, Any] | None]
    expected_state: Mapping[str, Mapping[str, Any] | None]


class _FrozenJsonMapping(MappingABC):
    __slots__ = ("__data",)

    def __init__(self, items):
        object.__setattr__(
            self,
            "_FrozenJsonMapping__data",
            MappingProxyType(dict(items)),
        )

    def __getitem__(self, key):
        return self.__data[key]

    def __iter__(self):
        return iter(self.__data)

    def __len__(self):
        return len(self.__data)

    def __repr__(self):
        return repr(self.__data)

    def __eq__(self, other):
        if not isinstance(other, MappingABC):
            return False
        return _thaw_json(self) == _thaw_json(other)

    def __setattr__(self, name, value):
        raise TypeError("classification snapshot mappings are immutable")

    def __deepcopy__(self, memo):
        return self


class _FrozenJsonSequence(SequenceABC):
    __slots__ = ("__data",)

    def __init__(self, items):
        object.__setattr__(self, "_FrozenJsonSequence__data", tuple(items))

    def __getitem__(self, index):
        return self.__data[index]

    def __len__(self):
        return len(self.__data)

    def __repr__(self):
        return repr(list(self.__data))

    def __eq__(self, other):
        if (
            isinstance(other, (str, bytes, bytearray))
            or not isinstance(other, SequenceABC)
        ):
            return False
        return _thaw_json(self) == _thaw_json(other)

    def __setattr__(self, name, value):
        raise TypeError("classification snapshot sequences are immutable")

    def __deepcopy__(self, memo):
        return self


def _freeze_json(value: Any) -> Any:
    if type(value) is dict:
        return _FrozenJsonMapping(
            (key, _freeze_json(item)) for key, item in value.items()
        )
    if type(value) is list:
        return _FrozenJsonSequence(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, MappingABC):
        return {key: _thaw_json(item) for key, item in value.items()}
    if (
        not isinstance(value, (str, bytes, bytearray))
        and isinstance(value, SequenceABC)
    ):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, init=False)
class _VerifiedHardOptoutEvidence:
    evidence: Mapping[str, Any]

    def __init__(self, *, evidence: dict[str, Any]):
        object.__setattr__(self, "evidence", _freeze_json(deepcopy(evidence)))


@dataclass(frozen=True, init=False)
class _VerifiedDeterministicEvidence:
    evidence: Mapping[str, Any]
    hard_optout: bool

    def __init__(self, *, evidence: dict[str, Any], hard_optout: bool):
        object.__setattr__(self, "evidence", _freeze_json(deepcopy(evidence)))
        object.__setattr__(self, "hard_optout", hard_optout)


@dataclass(frozen=True, init=False)
class _VerifiedRetainedTerminalEvidence:
    terminal_kind: str
    immutable_hash: str
    record_hash: str
    binding_hash: str
    aliases: Sequence[SourceAlias]

    def __init__(self):
        raise TypeError(
            "retained terminal evidence is constructed only by SourceCoordinator"
        )


@dataclass(frozen=True)
class _ClassificationTransactionPlan:
    result: Any
    identity_ref: Any
    identity_data: Mapping[str, Any]
    classification_ref: Any
    before_data: Mapping[str, Any] | None
    expected_data: Mapping[str, Any] | None
    ambiguous_error_type: type[SourceCoordinatorError] = SourceCoordinatorAmbiguous


@dataclass(frozen=True)
class _AuthorityCreatePlan:
    result: Any
    prerequisites: Sequence[tuple[Any, Mapping[str, Any]]]
    target_ref: Any
    before_data: Mapping[str, Any] | None
    expected_data: Mapping[str, Any] | None
    ambiguous_error_type: type[SourceCoordinatorError] = SourceCoordinatorAmbiguous


@dataclass(frozen=True)
class _MultiDocumentTransactionPlan:
    result: Any
    prerequisites: Sequence[tuple[Any, Mapping[str, Any] | None]]
    mutations: Sequence[
        tuple[Any, Mapping[str, Any] | None, Mapping[str, Any] | None]
    ]
    ambiguous_error_type: type[SourceCoordinatorError] = SourceCoordinatorAmbiguous


@dataclass(frozen=True)
class _DeferredClassificationError:
    error: SourceCoordinatorError


def resolve_source_coordinator_mode(environ: Mapping[str, str]) -> CoordinatorMode:
    value = environ.get(SOURCE_COORDINATOR_MODE_ENV)
    if type(value) is not str:
        return CoordinatorMode.DISABLED
    if value == CoordinatorMode.SHADOW.value:
        return CoordinatorMode.SHADOW
    if value == CoordinatorMode.ENFORCED.value:
        return CoordinatorMode.ENFORCED
    return CoordinatorMode.DISABLED


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    _validate_exact_json(value, active_containers=set())
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise SourceCoordinatorConfigError(
            "value is not canonical finite JSON"
        ) from exc


def _contains_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def normalize_source_alias(alias_type: str, value: str) -> SourceAlias:
    if type(alias_type) is not str or alias_type not in _SOURCE_ALIAS_TYPES:
        raise SourceCoordinatorConfigError("source alias type is unsupported")
    if type(value) is not str:
        raise SourceCoordinatorConfigError("source alias value must be a string")
    if _contains_control_character(value):
        raise SourceCoordinatorConfigError("source alias contains a control character")

    normalized = value.strip()
    if alias_type == "internet_message_id":
        while len(normalized) >= 2 and normalized[0] == "<" and normalized[-1] == ">":
            normalized = normalized[1:-1].strip()

    if not normalized:
        raise SourceCoordinatorConfigError("source alias value is empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceCoordinatorConfigError(
            "source alias value is not valid UTF-8"
        ) from exc
    if len(encoded) > MAX_SOURCE_ALIAS_BYTES:
        raise SourceCoordinatorConfigError("source alias value exceeds byte limit")
    return SourceAlias(alias_type=alias_type, value=normalized)


def source_alias_key(user_id: str, alias: SourceAlias) -> str:
    if type(user_id) is not str or not user_id:
        raise SourceCoordinatorConfigError("user id must be a non-empty string")
    if not isinstance(alias, SourceAlias):
        raise SourceCoordinatorConfigError("source alias is invalid")

    canonical = normalize_source_alias(alias.alias_type, alias.value)
    if (
        canonical.alias_type != alias.alias_type
        or canonical.value != alias.value
    ):
        raise SourceCoordinatorConfigError("source alias is not canonical")

    try:
        encoded = "\0".join(
            (_SOURCE_ALIAS_KEY_DOMAIN, user_id, alias.alias_type, alias.value)
        ).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceCoordinatorConfigError(
            "source alias key input is not valid UTF-8"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _normalized_value_hash(alias: SourceAlias) -> str:
    return canonical_json_hash(
        {
            "schemaVersion": _SOURCE_IDENTITY_SCHEMA_VERSION,
            "hashKind": _SOURCE_ALIAS_VALUE_HASH_KIND,
            "aliasType": alias.alias_type,
            "normalizedValue": alias.value,
        }
    )


def _is_sha256(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_exact_schema_version(value: Any, expected: int) -> bool:
    return type(value) is int and value == expected


def _is_aware_datetime(value: Any) -> bool:
    try:
        return (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        )
    except Exception:
        return False


def _alias_descriptor(alias: SourceAlias) -> dict[str, str]:
    return {
        "sourceAliasKey": alias.key,
        "aliasType": alias.alias_type,
        "normalizedValueHash": _normalized_value_hash(alias),
    }


def _alias_projection(
    alias: SourceAlias,
    *,
    canonical_source_id: str,
    created_at: Any,
) -> dict[str, Any]:
    return {
        "schemaVersion": _SOURCE_IDENTITY_SCHEMA_VERSION,
        **_alias_descriptor(alias),
        "canonicalSourceId": canonical_source_id,
        "createdAt": created_at,
    }


def _snapshot_data(snapshot: Any) -> dict[str, Any] | None:
    try:
        exists = snapshot.exists
        data = snapshot.to_dict() if exists else None
    except Exception as exc:
        raise SourceCoordinatorAmbiguous(
            "source authority snapshot is unreadable"
        ) from exc
    if not exists:
        return None
    if type(data) is not dict:
        raise SourceCoordinatorAmbiguous("source authority snapshot is malformed")
    return data


def _source_authority_revision_from_sync(
    data: Mapping[str, Any] | None,
    *,
    allow_missing: bool,
) -> int | None:
    if data is not None and type(data) is not dict:
        raise SourceCoordinatorAmbiguous(
            "source authority revision document is malformed"
        )
    has_revision = (
        data is not None and _SOURCE_AUTHORITY_REVISION_FIELD in data
    )
    revision = (
        data.get(_SOURCE_AUTHORITY_REVISION_FIELD) if has_revision else None
    )
    if revision is None:
        if has_revision or not allow_missing:
            raise SourceCoordinatorAmbiguous(
                "source authority revision is missing"
            )
    elif type(revision) is not int or revision < 1:
        raise SourceCoordinatorAmbiguous(
            "source authority revision is malformed"
        )
    if data is not None and _SOURCE_AUTHORITY_AUDIT_REVISION_FIELD in data:
        audited_revision = data[_SOURCE_AUTHORITY_AUDIT_REVISION_FIELD]
        if (
            type(audited_revision) is not int
            or audited_revision < 0
            or (revision is None and audited_revision != 0)
            or (revision is not None and audited_revision > revision)
        ):
            raise SourceCoordinatorAmbiguous(
                "source authority audit revision is malformed"
            )
    return revision


def _scan_cursor_datetime(value: str, *, field_name: str) -> datetime:
    if type(value) is not str or not value:
        raise SourceCoordinatorConfigError(
            f"{field_name} must be an aware ISO timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SourceCoordinatorConfigError(
            f"{field_name} must be an aware ISO timestamp"
        ) from exc
    if not _is_aware_datetime(parsed):
        raise SourceCoordinatorConfigError(
            f"{field_name} must be an aware ISO timestamp"
        )
    return parsed.astimezone(timezone.utc)


def _validate_user_id(user_id: str) -> None:
    _validate_document_id(user_id, field_name="user id")


def _validate_document_id(value: str, *, field_name: str) -> None:
    if type(value) is not str or not value:
        raise SourceCoordinatorConfigError(
            f"{field_name} must be a non-empty string"
        )
    if (
        value in {".", ".."}
        or "/" in value
        or _contains_control_character(value)
        or (len(value) >= 4 and value.startswith("__") and value.endswith("__"))
    ):
        raise SourceCoordinatorConfigError(f"{field_name} is not a safe document id")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceCoordinatorConfigError(
            f"{field_name} is not valid UTF-8"
        ) from exc
    if len(encoded) > _FIRESTORE_DOCUMENT_ID_MAX_BYTES:
        raise SourceCoordinatorConfigError(f"{field_name} exceeds byte limit")


def _validate_thread_id(thread_id: str | None) -> str | None:
    if thread_id is None:
        return None
    if type(thread_id) is not str:
        raise SourceCoordinatorConfigError("thread id must be a string or null")
    if not thread_id:
        return None
    _validate_document_id(thread_id, field_name="thread id")
    return thread_id


def _validate_exact_json(value: Any, *, active_containers: set[int]) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise SourceCoordinatorConfigError(
                "hydrated message contains a non-finite number"
            )
        return
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SourceCoordinatorConfigError(
                "hydrated message contains invalid UTF-8"
            ) from exc
        return
    if type(value) not in {dict, list}:
        raise SourceCoordinatorConfigError(
            "hydrated message must contain exact JSON values"
        )

    container_id = id(value)
    if container_id in active_containers:
        raise SourceCoordinatorConfigError("hydrated message contains a cycle")
    active_containers.add(container_id)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise SourceCoordinatorConfigError(
                        "hydrated message keys must be strings"
                    )
                _validate_exact_json(key, active_containers=active_containers)
                _validate_exact_json(item, active_containers=active_containers)
        else:
            for item in value:
                _validate_exact_json(item, active_containers=active_containers)
    except RecursionError as exc:
        raise SourceCoordinatorConfigError(
            "hydrated message exceeds nesting limit"
        ) from exc
    finally:
        active_containers.remove(container_id)


def _copy_exact_json_mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise SourceCoordinatorConfigError(f"{field_name} must be an exact mapping")
    _validate_exact_json(value, active_containers=set())
    return deepcopy(value)


def _validate_positive_integer(value: Any, *, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise SourceCoordinatorConfigError(f"{field_name} must be a positive integer")
    return value


def _validate_model_request_key(value: Any) -> str:
    if type(value) is not str or not value or _contains_control_character(value):
        raise SourceCoordinatorConfigError(
            "model request key must be a non-empty string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceCoordinatorConfigError(
            "model request key is not valid UTF-8"
        ) from exc
    if len(encoded) > _MODEL_REQUEST_KEY_MAX_BYTES:
        raise SourceCoordinatorConfigError("model request key exceeds byte limit")
    return value


def _sorted_semantic_items(value: Any, *, field_name: str) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise SourceCoordinatorConfigError(f"{field_name} must be an exact list")
    copied = []
    for item in value:
        if type(item) is not dict:
            raise SourceCoordinatorConfigError(
                f"{field_name} items must be exact mappings"
            )
        _validate_exact_json(item, active_containers=set())
        copied.append(deepcopy(item))
    return sorted(copied, key=_canonical_json_bytes)


def _normalize_complete_proposal(
    complete_proposal: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = _copy_exact_json_mapping(
        complete_proposal,
        field_name="complete proposal",
    )
    if (
        set(normalized) != _COMPLETE_PROPOSAL_FIELDS
        or not _is_exact_schema_version(
            normalized.get("schemaVersion"),
            _CLASSIFICATION_SNAPSHOT_SCHEMA_VERSION,
        )
        or type(normalized.get("transitionCandidates")) is not list
        or type(normalized.get("ordinaryObligations")) is not list
    ):
        raise SourceCoordinatorConfigError(
            "complete proposal schema is unsupported"
        )
    normalized["transitionCandidates"] = _sorted_semantic_items(
        normalized["transitionCandidates"],
        field_name="transition candidates",
    )
    normalized["ordinaryObligations"] = _sorted_semantic_items(
        normalized["ordinaryObligations"],
        field_name="ordinary obligations",
    )
    legal_transition_types = {
        "contact_optout",
        *_TERMINAL_CANDIDATE_TYPES,
        *_HUMAN_CANDIDATE_TYPES,
    }
    for candidate in normalized["transitionCandidates"]:
        if _candidate_type(candidate) not in legal_transition_types:
            raise SourceCoordinatorConfigError(
                "classification candidate is stored in an illegal lane"
            )
    for obligation in normalized["ordinaryObligations"]:
        if _candidate_type(obligation) not in _ORDINARY_CANDIDATE_TYPES:
            raise SourceCoordinatorConfigError(
                "classification obligation is stored in an illegal lane"
            )
    return normalized


def _candidate_type(candidate: Mapping[str, Any]) -> str:
    candidate_type = candidate.get("type")
    if type(candidate_type) is not str or not candidate_type:
        raise SourceCoordinatorConfigError(
            "classification candidate type must be a non-empty string"
        )
    return candidate_type


def _derive_classification_selection(
    *,
    canonical_source_id: str | None,
    complete_proposal: Mapping[str, Any],
    deterministic_hard_optout: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    normalized_candidates = []
    normalized_obligations = _sorted_semantic_items(
        complete_proposal.get("ordinaryObligations", []),
        field_name="ordinary obligations",
    )
    for candidate in _sorted_semantic_items(
        complete_proposal.get("transitionCandidates", []),
        field_name="transition candidates",
    ):
        candidate_type = _candidate_type(candidate)
        if candidate_type == "contact_optout":
            if deterministic_hard_optout:
                normalized_candidates.append(deepcopy(candidate))
            else:
                normalized_candidates.append(
                    {
                        "type": "needs_user_input",
                        "reason": "unverified_optout_review",
                        "sourceCandidateHash": canonical_json_hash(candidate),
                    }
                )
            continue
        if candidate_type in _TERMINAL_CANDIDATE_TYPES | _HUMAN_CANDIDATE_TYPES:
            normalized_candidates.append(deepcopy(candidate))
            continue
        if candidate_type in _ORDINARY_CANDIDATE_TYPES:
            normalized_obligations.append(deepcopy(candidate))
            continue
        raise SourceCoordinatorConfigError(
            "classification contains an unknown transition-shaped candidate"
        )

    normalized_candidates = sorted(
        normalized_candidates,
        key=_canonical_json_bytes,
    )
    normalized_obligations = sorted(
        normalized_obligations,
        key=_canonical_json_bytes,
    )
    hard_candidates = [
        candidate
        for candidate in normalized_candidates
        if candidate.get("type") == "contact_optout"
    ]
    terminal_candidates = [
        candidate
        for candidate in normalized_candidates
        if candidate.get("type") in _TERMINAL_CANDIDATE_TYPES
    ]
    human_candidates = [
        candidate
        for candidate in normalized_candidates
        if candidate.get("type") in _HUMAN_CANDIDATE_TYPES
    ]
    if hard_candidates:
        owner_kind = "contact_optout"
        selected_candidates = hard_candidates
    elif terminal_candidates:
        owner_kind = "terminal"
        selected_candidates = terminal_candidates
    elif human_candidates:
        owner_kind = "human_decision"
        selected_candidates = human_candidates
    else:
        owner_kind = "none"
        selected_candidates = []

    owner_key = None
    if owner_kind != "none" and canonical_source_id is not None:
        owner_key = canonical_json_hash(
            {
                "hashKind": _CLASSIFICATION_SELECTION_HASH_KIND,
                "canonicalSourceId": canonical_source_id,
                "ownerKind": owner_kind,
                "selectedCandidates": selected_candidates,
            }
        )
    selected_hashes = {
        canonical_json_hash(candidate) for candidate in selected_candidates
    }
    dominance = [
        {
            "candidateHash": canonical_json_hash(candidate),
            "outcome": (
                "selected"
                if canonical_json_hash(candidate) in selected_hashes
                else "dominated"
            ),
        }
        for candidate in normalized_candidates
    ]
    selection_snapshot = {
        "candidateTaxonomyVersion": _CANDIDATE_TAXONOMY_VERSION,
        "ownerKind": owner_kind,
        "ownerKey": owner_key,
        "selectedCandidates": deepcopy(selected_candidates),
        "candidateDominance": dominance,
        "transitionCandidatesHash": canonical_json_hash(normalized_candidates),
        "ordinaryObligationsHash": canonical_json_hash(normalized_obligations),
    }
    return normalized_candidates, normalized_obligations, selection_snapshot


def build_selection_snapshot(
    complete_proposal: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return a source-independent preview; it never grants owner authority."""
    normalized = _normalize_complete_proposal(complete_proposal)
    candidates, obligations, selection = _derive_classification_selection(
        canonical_source_id=None,
        complete_proposal=normalized,
        deterministic_hard_optout=False,
    )
    preview = deepcopy(selection)
    preview.pop("ownerKey", None)
    preview["transitionCandidates"] = candidates
    preview["ordinaryObligations"] = obligations
    return _freeze_json(preview)


def _transition_owner_immutable_material(
    classification_data: Mapping[str, Any],
) -> dict[str, Any]:
    selection = classification_data["selectionSnapshot"]
    owner_kind = selection.get("ownerKind")
    owner_key = selection.get("ownerKey")
    if (
        owner_kind not in _TRANSITION_OWNER_KINDS
        or (owner_kind == "none") != (owner_key is None)
        or (owner_key is not None and not _is_sha256(owner_key))
    ):
        raise SourceCoordinatorAmbiguous(
            "classification selection owner is malformed"
        )
    immutable = {
        "schemaVersion": _TRANSITION_OWNER_SCHEMA_VERSION,
        "canonicalSourceId": classification_data["canonicalSourceId"],
        "snapshotImmutableHash": classification_data["snapshotImmutableHash"],
        "selectionHash": classification_data["selectionHash"],
        "ownerKind": owner_kind,
        "ownerKey": owner_key,
    }
    immutable["ownerDecisionHash"] = canonical_json_hash(
        {
            "hashKind": _TRANSITION_OWNER_HASH_KIND,
            **immutable,
        }
    )
    return immutable


def _validate_transition_owner_document(
    data: Mapping[str, Any],
    *,
    canonical_source_id: str,
    classification_data: Mapping[str, Any] | None = None,
) -> None:
    if (
        type(data) is not dict
        or set(data) != _TRANSITION_OWNER_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _TRANSITION_OWNER_SCHEMA_VERSION,
        )
        or data.get("canonicalSourceId") != canonical_source_id
        or type(data.get("revision")) is not int
        or data.get("revision") != 1
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
    ):
        raise SourceCoordinatorAmbiguous("transition owner is malformed")
    expected_hash = canonical_json_hash(
        {
            "hashKind": _TRANSITION_OWNER_HASH_KIND,
            **{
                field: data[field]
                for field in (
                    "schemaVersion",
                    "canonicalSourceId",
                    "snapshotImmutableHash",
                    "selectionHash",
                    "ownerKind",
                    "ownerKey",
                )
            },
        }
    )
    if (
        data.get("ownerKind") not in _TRANSITION_OWNER_KINDS
        or (data.get("ownerKind") == "none")
        != (data.get("ownerKey") is None)
        or (
            data.get("ownerKey") is not None
            and not _is_sha256(data.get("ownerKey"))
        )
        or not _is_sha256(data.get("snapshotImmutableHash"))
        or not _is_sha256(data.get("selectionHash"))
        or data.get("ownerDecisionHash") != expected_hash
    ):
        raise SourceCoordinatorAmbiguous("transition owner hashes conflict")
    if classification_data is not None:
        expected = _transition_owner_immutable_material(classification_data)
        if any(data.get(field) != value for field, value in expected.items()):
            raise TransitionOwnerConflict(
                "transition owner conflicts with classification"
            )


def _work_dominance_outcome(
    *,
    lane: str,
    kind: str,
    payload_hash: str,
    owner_kind: str,
    selected_hashes: set[str],
) -> str:
    if lane == "ordinary":
        if kind == "generic_reply":
            if owner_kind == "terminal":
                return "delegate_terminal_policy"
            if owner_kind in {"contact_optout", "human_decision"}:
                return "dominated_no_send"
        return "preserve"
    if payload_hash in selected_hashes:
        return "delegate_owner"
    return "dominated_by_owner"


def _completion_contract(*, kind: str, dominance_outcome: str) -> dict[str, Any]:
    evidence_kind = {
        "delegate_owner": "owner_delegation",
        "delegate_terminal_policy": "terminal_policy_delegation",
        "dominated_by_owner": "selection_dominance",
        "dominated_no_send": "selection_dominance",
        "preserve": "work_completion",
    }[dominance_outcome]
    return {
        "schemaVersion": _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        "evidenceKind": evidence_kind,
        "workKind": kind,
    }


def _build_source_work_entries(
    *,
    canonical_source_id: str,
    classification_data: Mapping[str, Any],
    owner_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    selection = classification_data["selectionSnapshot"]
    selected_hashes = {
        canonical_json_hash(candidate)
        for candidate in selection["selectedCandidates"]
    }
    raw_items = [
        ("transition", deepcopy(item))
        for item in classification_data["transitionCandidates"]
    ] + [
        ("ordinary", deepcopy(item))
        for item in classification_data["ordinaryObligations"]
    ]
    raw_items.sort(
        key=lambda item: _canonical_json_bytes(
            {"lane": item[0], "payload": item[1]}
        )
    )
    occurrences: dict[str, int] = {}
    entries = []
    for lane, payload in raw_items:
        payload_hash = canonical_json_hash(payload)
        semantic_hash = canonical_json_hash(
            {"lane": lane, "payloadHash": payload_hash}
        )
        occurrence_ordinal = occurrences.get(semantic_hash, 0) + 1
        occurrences[semantic_hash] = occurrence_ordinal
        kind = _candidate_type(payload)
        dominance_outcome = _work_dominance_outcome(
            lane=lane,
            kind=kind,
            payload_hash=payload_hash,
            owner_kind=owner_data["ownerKind"],
            selected_hashes=selected_hashes,
        )
        work_key = canonical_json_hash(
            {
                "hashKind": _SOURCE_WORK_KEY_HASH_KIND,
                "canonicalSourceId": canonical_source_id,
                "snapshotImmutableHash": classification_data[
                    "snapshotImmutableHash"
                ],
                "selectionHash": classification_data["selectionHash"],
                "lane": lane,
                "payloadHash": payload_hash,
                "occurrenceOrdinal": occurrence_ordinal,
            }
        )
        entries.append(
            {
                "workKey": work_key,
                "lane": lane,
                "kind": kind,
                "payload": payload,
                "payloadHash": payload_hash,
                "occurrenceOrdinal": occurrence_ordinal,
                "selectedOwnerKind": owner_data["ownerKind"],
                "selectedOwnerKey": owner_data["ownerKey"],
                "dominanceOutcome": dominance_outcome,
                "completionContract": _completion_contract(
                    kind=kind,
                    dominance_outcome=dominance_outcome,
                ),
                "state": "pending",
                "resolutionEvidence": None,
                "resolutionEvidenceHash": None,
            }
        )
    return entries


def _source_work_resolution_evidence_hash(
    evidence: Mapping[str, Any],
) -> str:
    return canonical_json_hash(
        {
            "hashKind": _SOURCE_WORK_RESOLUTION_EVIDENCE_HASH_KIND,
            "evidence": evidence,
        }
    )


def _validate_source_work_entry_resolution(
    entry: Mapping[str, Any],
    *,
    canonical_source_id: str,
    ledger_hash: str,
    selection_hash: str,
    owner_decision_hash: str,
) -> None:
    state = entry["state"]
    evidence = entry["resolutionEvidence"]
    evidence_hash = entry["resolutionEvidenceHash"]
    if state in {"pending", "applying"}:
        if evidence is not None or evidence_hash is not None:
            raise SourceWorkLedgerConflict(
                "unsettled work entry contains resolution evidence"
            )
        return
    if type(evidence) is not dict or not _is_sha256(evidence_hash):
        raise SourceWorkLedgerConflict(
            "settled work entry lacks exact resolution evidence"
        )
    if evidence_hash != _source_work_resolution_evidence_hash(evidence):
        raise SourceWorkLedgerConflict("work resolution evidence hash conflicts")
    common_conflict = (
        not _is_exact_schema_version(
            evidence.get("schemaVersion"),
            _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        )
        or evidence.get("canonicalSourceId") != canonical_source_id
        or evidence.get("ledgerHash") != ledger_hash
        or evidence.get("workKey") != entry["workKey"]
        or evidence.get("payloadHash") != entry["payloadHash"]
        or evidence.get("workKind") != entry["kind"]
    )
    if common_conflict:
        raise SourceWorkLedgerConflict(
            "work resolution evidence conflicts with its ledger entry"
        )
    if state == "completed":
        valid = (
            set(evidence) == _COMPLETION_EVIDENCE_FIELDS
            and evidence.get("evidenceKind") == "work_completion"
            and entry["completionContract"]["evidenceKind"]
            == "work_completion"
            and _is_sha256(evidence.get("resultHash"))
        )
    elif state == "delegated":
        valid = (
            set(evidence) == _DELEGATION_EVIDENCE_FIELDS
            and evidence.get("evidenceKind")
            == entry["completionContract"]["evidenceKind"]
            and entry["dominanceOutcome"]
            in {"delegate_owner", "delegate_terminal_policy"}
            and _is_sha256(evidence.get("deferredBindingHash"))
        )
    else:
        valid = (
            set(evidence) == _DOMINANCE_EVIDENCE_FIELDS
            and evidence.get("evidenceKind") == "selection_dominance"
            and entry["completionContract"]["evidenceKind"]
            == "selection_dominance"
            and entry["dominanceOutcome"]
            in {"dominated_by_owner", "dominated_no_send"}
            and evidence.get("selectionHash") == selection_hash
            and evidence.get("ownerDecisionHash") == owner_decision_hash
            and evidence.get("dominatingOwnerKind")
            == entry["selectedOwnerKind"]
            and evidence.get("dominatingOwnerKey")
            == entry["selectedOwnerKey"]
            and evidence.get("dominanceOutcome")
            == entry["dominanceOutcome"]
        )
    if not valid:
        raise SourceWorkLedgerConflict(
            "work resolution evidence schema is unsupported"
        )


def _validate_completion_record(
    completion_record: Mapping[str, Any],
    *,
    work_kind: str,
) -> dict[str, Any]:
    copied = _copy_exact_json_mapping(
        completion_record,
        field_name="completion record",
    )
    if (
        set(copied) != _COMPLETION_RECORD_FIELDS
        or not _is_exact_schema_version(
            copied.get("schemaVersion"),
            _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        )
        or copied.get("evidenceKind") != "work_completion"
        or copied.get("workKind") != work_kind
        or not _is_sha256(copied.get("resultHash"))
    ):
        raise SourceCoordinatorConfigError(
            "completion record schema is unsupported for the work kind"
        )
    return copied


def _completion_resolution_evidence(
    *,
    canonical_source_id: str,
    ledger_hash: str,
    entry: Mapping[str, Any],
    completion_record: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schemaVersion": _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        "evidenceKind": "work_completion",
        "canonicalSourceId": canonical_source_id,
        "ledgerHash": ledger_hash,
        "workKey": entry["workKey"],
        "payloadHash": entry["payloadHash"],
        "workKind": entry["kind"],
        "resultHash": completion_record["resultHash"],
    }


def _source_deferred_work_immutable_material(
    *,
    canonical_source_id: str,
    ledger_hash: str,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    if entry["dominanceOutcome"] == "delegate_owner":
        wake_condition = "owner_adapter_ready"
    elif entry["dominanceOutcome"] == "delegate_terminal_policy":
        wake_condition = "terminal_policy_ready"
    else:
        raise SourceWorkTransitionConflict(
            "source work entry is not eligible for delegation"
        )
    if (
        entry["selectedOwnerKind"] not in _TRANSITION_OWNER_KINDS - {"none"}
        or not _is_sha256(entry["selectedOwnerKey"])
    ):
        raise SourceWorkTransitionConflict(
            "delegated work lacks a selected transition owner"
        )
    material = {
        "schemaVersion": _SOURCE_DEFERRED_WORK_SCHEMA_VERSION,
        "workKey": entry["workKey"],
        "canonicalSourceId": canonical_source_id,
        "ledgerHash": ledger_hash,
        "entryPayloadHash": entry["payloadHash"],
        "targetOwnerKind": entry["selectedOwnerKind"],
        "targetOwnerKey": entry["selectedOwnerKey"],
        "wakeCondition": wake_condition,
        "completionContract": deepcopy(entry["completionContract"]),
    }
    material["bindingHash"] = canonical_json_hash(
        {
            "hashKind": _SOURCE_DEFERRED_WORK_HASH_KIND,
            **material,
        }
    )
    return material


def _validate_source_deferred_work_document(
    data: Mapping[str, Any],
    *,
    expected_immutable: Mapping[str, Any],
) -> None:
    if (
        type(data) is not dict
        or set(data) != _SOURCE_DEFERRED_WORK_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _SOURCE_DEFERRED_WORK_SCHEMA_VERSION,
        )
        or data.get("state") != "deferred"
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
        or any(data.get(field) != value for field, value in expected_immutable.items())
    ):
        raise DeferredWorkConflict(
            "source deferred work conflicts with ledger authority"
        )


def _delegation_resolution_evidence(
    *,
    canonical_source_id: str,
    ledger_hash: str,
    entry: Mapping[str, Any],
    deferred_binding_hash: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        "evidenceKind": entry["completionContract"]["evidenceKind"],
        "canonicalSourceId": canonical_source_id,
        "ledgerHash": ledger_hash,
        "workKey": entry["workKey"],
        "payloadHash": entry["payloadHash"],
        "workKind": entry["kind"],
        "deferredBindingHash": deferred_binding_hash,
    }


def _dominance_resolution_evidence(
    *,
    canonical_source_id: str,
    ledger_data: Mapping[str, Any],
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    if entry["dominanceOutcome"] not in {
        "dominated_by_owner",
        "dominated_no_send",
    }:
        raise SourceWorkTransitionConflict(
            "source work entry is not dominated by the stored selection"
        )
    return {
        "schemaVersion": _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        "evidenceKind": "selection_dominance",
        "canonicalSourceId": canonical_source_id,
        "ledgerHash": ledger_data["ledgerHash"],
        "workKey": entry["workKey"],
        "payloadHash": entry["payloadHash"],
        "workKind": entry["kind"],
        "selectionHash": ledger_data["selectionHash"],
        "ownerDecisionHash": ledger_data["ownerDecisionHash"],
        "dominatingOwnerKind": entry["selectedOwnerKind"],
        "dominatingOwnerKey": entry["selectedOwnerKey"],
        "dominanceOutcome": entry["dominanceOutcome"],
    }


def _source_settlement_identity_hash(
    identity_data: Mapping[str, Any],
) -> str:
    return canonical_json_hash(
        {
            "hashKind": _SOURCE_SETTLEMENT_IDENTITY_HASH_KIND,
            "schemaVersion": identity_data["schemaVersion"],
            "canonicalSourceId": identity_data["canonicalSourceId"],
            "creationHash": identity_data["creationHash"],
            "threadId": identity_data["threadId"],
        }
    )


def _final_ledger_evidence_hash(
    ledger_data: Mapping[str, Any],
) -> str:
    if any(
        entry["state"] not in {"completed", "delegated", "dominated"}
        for entry in ledger_data["entries"]
    ):
        raise SourceSettlementNotReady(
            "source work ledger contains unsettled entries"
        )
    ordered_evidence = [
        {
            "workKey": entry["workKey"],
            "payloadHash": entry["payloadHash"],
            "state": entry["state"],
            "resolutionEvidenceHash": entry["resolutionEvidenceHash"],
        }
        for entry in ledger_data["entries"]
    ]
    return canonical_json_hash(
        {
            "hashKind": _FINAL_LEDGER_EVIDENCE_HASH_KIND,
            "ledgerHash": ledger_data["ledgerHash"],
            "entries": ordered_evidence,
        }
    )


def _source_settlement_hash_material(
    data: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "hashKind": _SOURCE_SETTLEMENT_HASH_KIND,
        **{
            field: data[field]
            for field in (
                "schemaVersion",
                "canonicalSourceId",
                "identityHash",
                "snapshotImmutableHash",
                "selectionHash",
                "ownerDecisionHash",
                "ledgerHash",
                "finalLedgerEvidenceHash",
                "threadHeadBinding",
                "aliases",
                "aliasSetHash",
                "settlementRevision",
            )
        },
    }


def _source_settlement_immutable_material(
    *,
    canonical_source_id: str,
    identity_data: Mapping[str, Any],
    classification_data: Mapping[str, Any],
    owner_data: Mapping[str, Any],
    ledger_data: Mapping[str, Any],
    thread_head_binding: Mapping[str, Any] | None,
    aliases: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    alias_list = [deepcopy(dict(alias)) for alias in aliases]
    material = {
        "schemaVersion": _SOURCE_SETTLEMENT_SCHEMA_VERSION,
        "canonicalSourceId": canonical_source_id,
        "identityHash": _source_settlement_identity_hash(identity_data),
        "snapshotImmutableHash": classification_data["snapshotImmutableHash"],
        "selectionHash": classification_data["selectionHash"],
        "ownerDecisionHash": owner_data["ownerDecisionHash"],
        "ledgerHash": ledger_data["ledgerHash"],
        "finalLedgerEvidenceHash": _final_ledger_evidence_hash(ledger_data),
        "threadHeadBinding": (
            None
            if thread_head_binding is None
            else deepcopy(dict(thread_head_binding))
        ),
        "aliases": alias_list,
        "aliasSetHash": canonical_json_hash(
            {
                "hashKind": _SOURCE_SETTLEMENT_ALIAS_SET_HASH_KIND,
                "aliases": alias_list,
            }
        ),
        "settlementRevision": 1,
    }
    material["settlementHash"] = canonical_json_hash(
        _source_settlement_hash_material(material)
    )
    return material


def _validate_source_settlement_document(
    data: Mapping[str, Any],
    *,
    canonical_source_id: str,
    identity_data: Mapping[str, Any],
    classification_data: Mapping[str, Any],
    owner_data: Mapping[str, Any],
    ledger_data: Mapping[str, Any],
    current_aliases: Sequence[Mapping[str, str]],
) -> None:
    if (
        type(data) is not dict
        or set(data) != _SOURCE_SETTLEMENT_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _SOURCE_SETTLEMENT_SCHEMA_VERSION,
        )
        or data.get("canonicalSourceId") != canonical_source_id
        or type(data.get("settlementRevision")) is not int
        or data.get("settlementRevision") != 1
        or not _is_aware_datetime(data.get("settledAt"))
        or not _is_sha256(data.get("settlementHash"))
    ):
        raise SourceSettlementConflict("source settlement is malformed")
    aliases = data.get("aliases")
    if type(aliases) is not list or not aliases:
        raise SourceSettlementConflict("source settlement alias set is malformed")
    seen = set()
    for descriptor in aliases:
        if (
            type(descriptor) is not dict
            or set(descriptor)
            != {"sourceAliasKey", "aliasType", "normalizedValueHash"}
            or not _is_sha256(descriptor.get("sourceAliasKey"))
            or descriptor.get("aliasType") not in _SOURCE_ALIAS_TYPES
            or not _is_sha256(descriptor.get("normalizedValueHash"))
            or descriptor["sourceAliasKey"] in seen
        ):
            raise SourceSettlementConflict(
                "source settlement alias set is malformed"
            )
        seen.add(descriptor["sourceAliasKey"])
    if aliases != sorted(aliases, key=lambda item: item["sourceAliasKey"]):
        raise SourceSettlementConflict("source settlement aliases are unordered")
    current_by_key = {
        descriptor["sourceAliasKey"]: descriptor for descriptor in current_aliases
    }
    if any(
        current_by_key.get(descriptor["sourceAliasKey"]) != descriptor
        for descriptor in aliases
    ):
        raise SourceSettlementConflict(
            "retained settlement aliases conflict with source identity"
        )
    thread_head_binding = data.get("threadHeadBinding")
    if owner_data["ownerKind"] == "none":
        if thread_head_binding is not None:
            raise SourceSettlementConflict(
                "none-owner settlement retains a thread-head binding"
            )
    else:
        try:
            _validate_blocker(thread_head_binding)
        except SourceCoordinatorError as error:
            raise SourceSettlementConflict(
                "source settlement thread-head binding is malformed"
            ) from error
        expected_head_hash = canonical_json_hash(
            _thread_head_hash_material(
                {
                    "schemaVersion": _THREAD_HEAD_SCHEMA_VERSION,
                    "threadId": identity_data["threadId"],
                    "threadHeadRevision": thread_head_binding[
                        "threadHeadRevision"
                    ],
                    "activeOwnerKey": thread_head_binding["ownerKey"],
                    "activeOwnerKind": thread_head_binding["ownerKind"],
                    "activeCanonicalSourceId": thread_head_binding[
                        "canonicalSourceId"
                    ],
                    "activeGeneration": thread_head_binding["generation"],
                    "activeState": "active",
                }
            )
        )
        if (
            thread_head_binding["canonicalSourceId"] != canonical_source_id
            or thread_head_binding["ownerKind"] != owner_data["ownerKind"]
            or thread_head_binding["ownerKey"] != owner_data["ownerKey"]
            or thread_head_binding["threadHeadRevision"]
            != (2 * thread_head_binding["generation"]) - 1
            or thread_head_binding["headHash"] != expected_head_hash
        ):
            raise SourceSettlementConflict(
                "source settlement thread-head binding conflicts with retained authority"
            )
    expected_bindings = {
        "identityHash": _source_settlement_identity_hash(identity_data),
        "snapshotImmutableHash": classification_data["snapshotImmutableHash"],
        "selectionHash": classification_data["selectionHash"],
        "ownerDecisionHash": owner_data["ownerDecisionHash"],
        "ledgerHash": ledger_data["ledgerHash"],
        "finalLedgerEvidenceHash": _final_ledger_evidence_hash(ledger_data),
    }
    expected_alias_hash = canonical_json_hash(
        {
            "hashKind": _SOURCE_SETTLEMENT_ALIAS_SET_HASH_KIND,
            "aliases": aliases,
        }
    )
    expected_settlement_hash = canonical_json_hash(
        _source_settlement_hash_material(data)
    )
    if (
        any(data.get(field) != value for field, value in expected_bindings.items())
        or data.get("aliasSetHash") != expected_alias_hash
        or data.get("settlementHash") != expected_settlement_hash
    ):
        raise SourceSettlementConflict(
            "source settlement conflicts with retained authority"
        )


def _processed_alias_projection(
    *,
    descriptor: Mapping[str, str],
    canonical_source_id: str,
    settlement_data: Mapping[str, Any],
    processed_at: datetime,
) -> dict[str, Any]:
    return {
        "schemaVersion": _PROCESSED_ALIAS_SCHEMA_VERSION,
        **deepcopy(dict(descriptor)),
        "canonicalSourceId": canonical_source_id,
        "settlementRevision": settlement_data["settlementRevision"],
        "settlementHash": settlement_data["settlementHash"],
        "processedAt": processed_at,
    }


def _validate_processed_alias_projection(
    data: Mapping[str, Any],
    *,
    descriptor: Mapping[str, str],
    canonical_source_id: str,
    settlement_data: Mapping[str, Any],
) -> None:
    if (
        type(data) is not dict
        or set(data) != _PROCESSED_ALIAS_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _PROCESSED_ALIAS_SCHEMA_VERSION,
        )
        or not _is_aware_datetime(data.get("processedAt"))
    ):
        raise SourceSettlementConflict(
            "processed alias projection is malformed"
        )
    expected = _processed_alias_projection(
        descriptor=descriptor,
        canonical_source_id=canonical_source_id,
        settlement_data=settlement_data,
        processed_at=data["processedAt"],
    )
    if data != expected:
        raise SourceSettlementConflict(
            "processed alias projection conflicts with settlement"
        )


def _validate_source_work_transition_bindings(
    *,
    ledger_hash: str,
    work_key: str,
    payload_hash: str,
) -> None:
    if not _is_sha256(ledger_hash):
        raise SourceCoordinatorConfigError("ledger hash must be a full hash")
    if not _is_sha256(work_key):
        raise SourceCoordinatorConfigError("work key must be a full hash")
    if not _is_sha256(payload_hash):
        raise SourceCoordinatorConfigError("payload hash must be a full hash")


def _find_bound_source_work_entry(
    ledger_data: Mapping[str, Any],
    *,
    ledger_hash: str,
    work_key: str,
    payload_hash: str,
) -> tuple[int, Mapping[str, Any]]:
    if ledger_data["ledgerHash"] != ledger_hash:
        raise SourceWorkTransitionConflict(
            "caller ledger hash conflicts with source authority"
        )
    matches = [
        (index, entry)
        for index, entry in enumerate(ledger_data["entries"])
        if entry["workKey"] == work_key
    ]
    if len(matches) != 1 or matches[0][1]["payloadHash"] != payload_hash:
        raise SourceWorkTransitionConflict(
            "caller work binding conflicts with source ledger"
        )
    return matches[0]


def _source_work_ledger_immutable_material(
    *,
    canonical_source_id: str,
    classification_data: Mapping[str, Any],
    owner_data: Mapping[str, Any],
) -> dict[str, Any]:
    entries = _build_source_work_entries(
        canonical_source_id=canonical_source_id,
        classification_data=classification_data,
        owner_data=owner_data,
    )
    if len(entries) > MAX_SOURCE_WORK_ENTRIES:
        raise SourceWorkLedgerLimitExceeded(
            "source work entry limit exceeded"
        )
    immutable_entries = [
        {
            field: entry[field]
            for field in sorted(_SOURCE_WORK_ENTRY_IMMUTABLE_FIELDS)
        }
        for entry in entries
    ]
    ledger_hash = canonical_json_hash(
        {
            "hashKind": _SOURCE_WORK_LEDGER_HASH_KIND,
            "canonicalSourceId": canonical_source_id,
            "completeProposalHash": classification_data["completeProposalHash"],
            "snapshotImmutableHash": classification_data["snapshotImmutableHash"],
            "selectionHash": classification_data["selectionHash"],
            "ownerDecisionHash": owner_data["ownerDecisionHash"],
            "entries": immutable_entries,
        }
    )
    material = {
        "schemaVersion": _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        "canonicalSourceId": canonical_source_id,
        "completeProposalHash": classification_data["completeProposalHash"],
        "snapshotImmutableHash": classification_data["snapshotImmutableHash"],
        "selectionHash": classification_data["selectionHash"],
        "ownerDecisionHash": owner_data["ownerDecisionHash"],
        "entries": entries,
        "entryCount": len(entries),
        "ledgerHash": ledger_hash,
        "revision": 1,
    }
    if len(_canonical_json_bytes(material)) > MAX_SOURCE_WORK_LEDGER_BYTES:
        raise SourceWorkLedgerLimitExceeded(
            "source work ledger exceeds canonical byte limit"
        )
    planned_transaction_writes = 1
    if planned_transaction_writes > MAX_SOURCE_WORK_TRANSACTION_WRITES:
        raise SourceWorkLedgerLimitExceeded(
            "source work ledger transaction write limit exceeded"
        )
    return material


def _validate_source_work_ledger_document(
    data: Mapping[str, Any],
    *,
    canonical_source_id: str,
    classification_data: Mapping[str, Any],
    owner_data: Mapping[str, Any],
) -> None:
    if (
        type(data) is not dict
        or set(data) != _SOURCE_WORK_LEDGER_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _SOURCE_WORK_LEDGER_SCHEMA_VERSION,
        )
        or data.get("canonicalSourceId") != canonical_source_id
        or type(data.get("revision")) is not int
        or data.get("revision") < 1
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
        or type(data.get("entries")) is not list
        or type(data.get("entryCount")) is not int
        or data.get("entryCount") != len(data.get("entries", []))
    ):
        raise SourceCoordinatorAmbiguous("source work ledger is malformed")
    expected = _source_work_ledger_immutable_material(
        canonical_source_id=canonical_source_id,
        classification_data=classification_data,
        owner_data=owner_data,
    )
    if any(
        data.get(field) != expected[field]
        for field in (
            "schemaVersion",
            "canonicalSourceId",
            "completeProposalHash",
            "snapshotImmutableHash",
            "selectionHash",
            "ownerDecisionHash",
            "entryCount",
            "ledgerHash",
        )
    ):
        raise SourceWorkLedgerConflict(
            "source work ledger conflicts with authority"
        )
    expected_entries = expected["entries"]
    if len(data["entries"]) != len(expected_entries):
        raise SourceWorkLedgerConflict("source work ledger entries conflict")
    for stored, initial in zip(data["entries"], expected_entries):
        if (
            type(stored) is not dict
            or set(stored) != _SOURCE_WORK_ENTRY_FIELDS
            or stored.get("state")
            not in {"pending", "applying", "completed", "delegated", "dominated"}
            or any(
                stored.get(field) != value
                for field, value in initial.items()
                if field in _SOURCE_WORK_ENTRY_IMMUTABLE_FIELDS
            )
        ):
            raise SourceWorkLedgerConflict(
                "source work ledger entry conflicts with authority"
            )
        _validate_source_work_entry_resolution(
            stored,
            canonical_source_id=canonical_source_id,
            ledger_hash=data["ledgerHash"],
            selection_hash=data["selectionHash"],
            owner_decision_hash=data["ownerDecisionHash"],
        )


def _canonical_datetime_token(value: datetime, *, field_name: str) -> str:
    if not _is_aware_datetime(value):
        raise SourceCoordinatorConfigError(f"{field_name} must be an aware datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _thread_head_hash_material(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hashKind": _THREAD_HEAD_HASH_KIND,
        "schemaVersion": data["schemaVersion"],
        "threadId": data["threadId"],
        "threadHeadRevision": data["threadHeadRevision"],
        "activeOwnerKey": data["activeOwnerKey"],
        "activeOwnerKind": data["activeOwnerKind"],
        "activeCanonicalSourceId": data["activeCanonicalSourceId"],
        "activeGeneration": data["activeGeneration"],
        "activeState": data["activeState"],
    }


def _build_thread_head_document(
    *,
    thread_id: str,
    canonical_source_id: str | None,
    owner_data: Mapping[str, Any] | None,
    generation: int,
    state: str,
    revision: int,
    now: datetime,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    if state == "clear":
        owner_kind = None
        owner_key = None
        canonical_source_id = None
    else:
        if owner_data is None or canonical_source_id is None:
            raise ThreadTransitionConflict("active thread head requires an owner")
        owner_kind = owner_data["ownerKind"]
        owner_key = owner_data["ownerKey"]
        if owner_kind == "none" or owner_key is None:
            raise ThreadTransitionConflict(
                "explicit none owner cannot hold a thread transition head"
            )
    document = {
        "schemaVersion": _THREAD_HEAD_SCHEMA_VERSION,
        "threadId": thread_id,
        "threadHeadRevision": revision,
        "activeOwnerKey": owner_key,
        "activeOwnerKind": owner_kind,
        "activeCanonicalSourceId": canonical_source_id,
        "activeGeneration": generation,
        "activeState": state,
    }
    document["headHash"] = canonical_json_hash(
        _thread_head_hash_material(document)
    )
    document["createdAt"] = now if created_at is None else created_at
    document["updatedAt"] = now
    return document


def _validate_thread_head_document(
    data: Mapping[str, Any],
    *,
    thread_id: str,
) -> None:
    if (
        type(data) is not dict
        or set(data) != _THREAD_HEAD_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _THREAD_HEAD_SCHEMA_VERSION,
        )
        or data.get("threadId") != thread_id
        or type(data.get("threadHeadRevision")) is not int
        or data.get("threadHeadRevision") < 1
        or type(data.get("activeGeneration")) is not int
        or data.get("activeGeneration") < 0
        or data.get("activeState") not in {"active", "releasing", "clear"}
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
    ):
        raise SourceCoordinatorAmbiguous("thread transition head is malformed")
    if data["activeState"] == "clear":
        if any(
            data.get(field) is not None
            for field in (
                "activeOwnerKey",
                "activeOwnerKind",
                "activeCanonicalSourceId",
            )
        ):
            raise SourceCoordinatorAmbiguous("clear thread head retains an owner")
    elif (
        data.get("activeOwnerKind") not in _TRANSITION_OWNER_KINDS - {"none"}
        or not _is_sha256(data.get("activeOwnerKey"))
        or type(data.get("activeCanonicalSourceId")) is not str
        or not data.get("activeCanonicalSourceId")
        or data.get("activeGeneration") < 1
    ):
        raise SourceCoordinatorAmbiguous("active thread head owner is malformed")
    expected_hash = canonical_json_hash(_thread_head_hash_material(data))
    if data.get("headHash") != expected_hash:
        raise ThreadTransitionConflict("thread transition head hash conflicts")


def _blocker_from_head(data: Mapping[str, Any]) -> dict[str, Any]:
    if data["activeState"] not in {"active", "releasing"}:
        raise ThreadTransitionConflict("clear thread head cannot block a source")
    return {
        "canonicalSourceId": data["activeCanonicalSourceId"],
        "ownerKind": data["activeOwnerKind"],
        "ownerKey": data["activeOwnerKey"],
        "generation": data["activeGeneration"],
        "threadHeadRevision": data["threadHeadRevision"],
        "headHash": data["headHash"],
    }


def _validate_blocker(value: Any) -> None:
    if (
        type(value) is not dict
        or set(value) != _BLOCKER_FIELDS
        or type(value.get("canonicalSourceId")) is not str
        or not value.get("canonicalSourceId")
        or value.get("ownerKind") not in _TRANSITION_OWNER_KINDS - {"none"}
        or not _is_sha256(value.get("ownerKey"))
        or type(value.get("generation")) is not int
        or value.get("generation") < 1
        or type(value.get("threadHeadRevision")) is not int
        or value.get("threadHeadRevision") < 1
        or not _is_sha256(value.get("headHash"))
    ):
        raise SourceCoordinatorAmbiguous("thread blocker evidence is malformed")


def _pending_admission_hash_material(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "hashKind": _PENDING_ADMISSION_HASH_KIND,
        "schemaVersion": data["schemaVersion"],
        "canonicalSourceId": data["canonicalSourceId"],
        "threadId": data["threadId"],
        "identityCreationHash": data["identityCreationHash"],
        "snapshotImmutableHash": data["snapshotImmutableHash"],
        "selectionHash": data["selectionHash"],
        "ownerDecisionHash": data["ownerDecisionHash"],
        "ledgerHash": data["ledgerHash"],
        "ownerKind": data["ownerKind"],
        "ownerKey": data["ownerKey"],
        "savedHistoryBindingHash": data["savedHistoryBindingHash"],
        "indexBindingHash": data["indexBindingHash"],
        "receivedAt": _canonical_datetime_token(
            data["receivedAt"],
            field_name="received at",
        ),
        "sentAt": _canonical_datetime_token(
            data["sentAt"],
            field_name="sent at",
        ),
    }


def _pending_admission_immutable_material(
    *,
    canonical_source_id: str,
    thread_id: str,
    identity_data: Mapping[str, Any],
    classification_data: Mapping[str, Any],
    owner_data: Mapping[str, Any],
    ledger_data: Mapping[str, Any],
    received_at: datetime,
    sent_at: datetime,
    saved_history_binding: Mapping[str, Any],
    index_binding: Mapping[str, Any],
) -> dict[str, Any]:
    _canonical_datetime_token(received_at, field_name="received at")
    _canonical_datetime_token(sent_at, field_name="sent at")
    history_copy = _copy_exact_json_mapping(
        saved_history_binding,
        field_name="saved history binding",
    )
    index_copy = _copy_exact_json_mapping(
        index_binding,
        field_name="index binding",
    )
    material = {
        "schemaVersion": _PENDING_ADMISSION_SCHEMA_VERSION,
        "canonicalSourceId": canonical_source_id,
        "threadId": thread_id,
        "identityCreationHash": identity_data["creationHash"],
        "snapshotImmutableHash": classification_data["snapshotImmutableHash"],
        "selectionHash": classification_data["selectionHash"],
        "ownerDecisionHash": owner_data["ownerDecisionHash"],
        "ledgerHash": ledger_data["ledgerHash"],
        "ownerKind": owner_data["ownerKind"],
        "ownerKey": owner_data["ownerKey"],
        "savedHistoryBinding": history_copy,
        "savedHistoryBindingHash": canonical_json_hash(history_copy),
        "indexBinding": index_copy,
        "indexBindingHash": canonical_json_hash(index_copy),
        "receivedAt": received_at,
        "sentAt": sent_at,
    }
    material["admissionHash"] = canonical_json_hash(
        _pending_admission_hash_material(material)
    )
    return material


def _validate_pending_admission_document(
    data: Mapping[str, Any],
    *,
    canonical_source_id: str,
    thread_id: str,
    expected_immutable: Mapping[str, Any] | None = None,
) -> None:
    if (
        type(data) is not dict
        or set(data) != _PENDING_ADMISSION_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _PENDING_ADMISSION_SCHEMA_VERSION,
        )
        or data.get("canonicalSourceId") != canonical_source_id
        or data.get("threadId") != thread_id
        or data.get("ownerKind") not in _TRANSITION_OWNER_KINDS
        or (data.get("ownerKind") == "none") != (data.get("ownerKey") is None)
        or (
            data.get("ownerKey") is not None
            and not _is_sha256(data.get("ownerKey"))
        )
        or any(
            not _is_sha256(data.get(field))
            for field in (
                "identityCreationHash",
                "snapshotImmutableHash",
                "selectionHash",
                "ownerDecisionHash",
                "ledgerHash",
                "savedHistoryBindingHash",
                "indexBindingHash",
                "admissionHash",
            )
        )
        or not _is_aware_datetime(data.get("receivedAt"))
        or not _is_aware_datetime(data.get("sentAt"))
        or type(data.get("revision")) is not int
        or data.get("revision") < 1
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
        or data.get("admissionState")
        not in {"pending", "blocked", "processing", "settled"}
        or data.get("blockedLifecycleState")
        not in {
            None,
            "blocked",
            "eligible",
            "claimed",
            "settled",
            "settled_as_new_blocker",
        }
        or data.get("wakeState")
        not in {"none", "eligible", "claimed", "consumed"}
    ):
        raise SourceCoordinatorAmbiguous("pending admission is malformed")
    try:
        history_hash = canonical_json_hash(data["savedHistoryBinding"])
        index_hash = canonical_json_hash(data["indexBinding"])
        admission_hash = canonical_json_hash(_pending_admission_hash_material(data))
    except SourceCoordinatorError:
        raise
    except Exception as error:
        raise SourceCoordinatorAmbiguous(
            "pending admission immutable material is unreadable"
        ) from error
    if (
        history_hash != data["savedHistoryBindingHash"]
        or index_hash != data["indexBindingHash"]
        or admission_hash != data["admissionHash"]
    ):
        raise PendingAdmissionConflict("pending admission hashes conflict")
    for blocker_field in ("initialBlocker", "currentBlocker"):
        blocker = data[blocker_field]
        if blocker is not None:
            _validate_blocker(blocker)
    state = data["admissionState"]
    lifecycle = data["blockedLifecycleState"]
    wake_state = data["wakeState"]
    wake_generation = data["wakeGeneration"]
    wake_token = data["wakeToken"]
    wake_claim_id = data["wakeClaimId"]
    if wake_state == "none":
        wake_valid = (
            wake_generation is None
            and wake_token is None
            and wake_claim_id is None
        )
    elif wake_state == "eligible":
        wake_valid = (
            type(wake_generation) is int
            and wake_generation > 0
            and _is_sha256(wake_token)
            and wake_claim_id is None
            and state == "blocked"
            and lifecycle == "eligible"
        )
    elif wake_state == "claimed":
        wake_valid = (
            type(wake_generation) is int
            and wake_generation > 0
            and _is_sha256(wake_token)
            and type(wake_claim_id) is str
            and bool(wake_claim_id)
            and lifecycle == "claimed"
        )
    else:
        wake_valid = (
            type(wake_generation) is int
            and wake_generation > 0
            and _is_sha256(wake_token)
            and type(wake_claim_id) is str
            and bool(wake_claim_id)
            and lifecycle in {"settled", "settled_as_new_blocker"}
        )
    if not wake_valid:
        raise SourceCoordinatorAmbiguous("pending admission wake state is malformed")
    if state == "pending":
        state_valid = (
            lifecycle is None
            and data["initialBlocker"] is None
            and data["currentBlocker"] is None
            and wake_state == "none"
        )
    elif state == "blocked":
        state_valid = (
            lifecycle in {"blocked", "eligible", "claimed"}
            and data["initialBlocker"] is not None
            and data["currentBlocker"] is not None
        )
    elif lifecycle is None:
        state_valid = (
            data["initialBlocker"] is None
            and data["currentBlocker"] is None
            and wake_state == "none"
        )
    else:
        state_valid = (
            lifecycle in {"settled", "settled_as_new_blocker"}
            and data["initialBlocker"] is not None
            and data["currentBlocker"] is not None
            and wake_state == "consumed"
        )
    if not state_valid:
        raise SourceCoordinatorAmbiguous("pending admission state conflicts")
    if expected_immutable is not None and any(
        data.get(field) != value for field, value in expected_immutable.items()
    ):
        raise PendingAdmissionConflict(
            "pending admission conflicts with stored source authority"
        )


def _validate_source_resume_bindings(
    admission: Mapping[str, Any],
    *,
    canonical_source_id: str,
    thread_id: str,
) -> None:
    history_binding = admission["savedHistoryBinding"]
    index_binding = admission["indexBinding"]
    if (
        type(history_binding) is not dict
        or set(history_binding) != _SAVED_HISTORY_BINDING_FIELDS
        or not _is_exact_schema_version(history_binding.get("schemaVersion"), 1)
        or history_binding.get("canonicalSourceId") != canonical_source_id
        or history_binding.get("threadId") != thread_id
        or history_binding.get("historyDocumentId") != canonical_source_id
        or not _is_sha256(history_binding.get("historyHash"))
    ):
        raise SourceCoordinatorAmbiguous(
            "durable source history binding is malformed"
        )
    if (
        type(index_binding) is not dict
        or set(index_binding) != _SOURCE_INDEX_BINDING_FIELDS
        or not _is_exact_schema_version(index_binding.get("schemaVersion"), 1)
        or index_binding.get("canonicalSourceId") != canonical_source_id
        or index_binding.get("threadId") != thread_id
        or index_binding.get("identityDocumentId") != canonical_source_id
    ):
        raise SourceCoordinatorAmbiguous(
            "durable source index binding is malformed"
        )


def _validate_retained_source_history_document(
    data: Mapping[str, Any],
    *,
    canonical_source_id: str,
    thread_id: str,
    expected_history_hash: str,
) -> None:
    if type(data) is not dict:
        raise SourceCoordinatorAmbiguous(
            "durable source history document is malformed"
        )
    retained = deepcopy(data)
    retained_canonical_source_id = retained.pop("canonicalSourceId", None)
    retained_history_hash = retained.pop("historyHash", None)
    retained_semantic_hash = retained.pop("semanticHistoryHash", None)
    retained_semantic_message = deepcopy(retained)
    retained_headers = retained_semantic_message.get("headers")
    if isinstance(retained_headers, dict):
        retained_headers.pop("internetMessageId", None)
    retained_envelope = retained_semantic_message.get("sourceMessage")
    if isinstance(retained_envelope, dict):
        retained_envelope.pop("graphMessageId", None)
        retained_envelope.pop("internetMessageId", None)
    expected_full_hash = canonical_json_hash(
        {
            "schemaVersion": 1,
            "canonicalSourceId": canonical_source_id,
            "threadId": thread_id,
            "message": retained,
        }
    )
    expected_semantic_hash = canonical_json_hash(
        {
            "schemaVersion": 1,
            "canonicalSourceId": canonical_source_id,
            "threadId": thread_id,
            "message": retained_semantic_message,
        }
    )
    if (
        retained_canonical_source_id != canonical_source_id
        or retained_history_hash != expected_full_hash
        or retained_history_hash != expected_history_hash
        or retained_semantic_hash != expected_semantic_hash
    ):
        raise SourceCoordinatorAmbiguous(
            "durable source history conflicts with admission authority"
        )


def _initial_pending_admission_document(
    immutable: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    return {
        **deepcopy(dict(immutable)),
        "admissionState": "pending",
        "blockedLifecycleState": None,
        "initialBlocker": None,
        "currentBlocker": None,
        "wakeGeneration": None,
        "wakeToken": None,
        "wakeState": "none",
        "wakeClaimId": None,
        "revision": 1,
        "createdAt": now,
        "updatedAt": now,
    }


def _validate_admission_authority_bindings(
    admission: Mapping[str, Any],
    *,
    identity_data: Mapping[str, Any],
    classification_data: Mapping[str, Any],
    owner_data: Mapping[str, Any],
    ledger_data: Mapping[str, Any],
) -> None:
    expected = {
        "identityCreationHash": identity_data["creationHash"],
        "snapshotImmutableHash": classification_data["snapshotImmutableHash"],
        "selectionHash": classification_data["selectionHash"],
        "ownerDecisionHash": owner_data["ownerDecisionHash"],
        "ledgerHash": ledger_data["ledgerHash"],
        "ownerKind": owner_data["ownerKind"],
        "ownerKey": owner_data["ownerKey"],
    }
    if any(admission.get(field) != value for field, value in expected.items()):
        raise PendingAdmissionConflict(
            "pending admission authority bindings conflict"
        )


def _blocked_projection_from_admission(
    admission: Mapping[str, Any],
    *,
    now: datetime,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    if admission["currentBlocker"] is None:
        raise ThreadTransitionConflict(
            "blocked projection requires current blocker evidence"
        )
    return {
        "schemaVersion": _BLOCKED_SOURCE_SCHEMA_VERSION,
        "canonicalSourceId": admission["canonicalSourceId"],
        "threadId": admission["threadId"],
        "admissionHash": admission["admissionHash"],
        "admissionRevision": admission["revision"],
        "receivedAt": admission["receivedAt"],
        "sentAt": admission["sentAt"],
        "blockedLifecycleState": admission["blockedLifecycleState"],
        "currentBlocker": deepcopy(admission["currentBlocker"]),
        "wakeGeneration": admission["wakeGeneration"],
        "wakeState": admission["wakeState"],
        "createdAt": now if created_at is None else created_at,
        "updatedAt": now,
    }


def _validate_blocked_projection_document(
    data: Mapping[str, Any],
    *,
    admission: Mapping[str, Any],
) -> None:
    if (
        type(data) is not dict
        or set(data) != _BLOCKED_SOURCE_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _BLOCKED_SOURCE_SCHEMA_VERSION,
        )
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
    ):
        raise SourceCoordinatorAmbiguous("blocked source projection is malformed")
    expected = _blocked_projection_from_admission(
        admission,
        now=data["updatedAt"],
        created_at=data["createdAt"],
    )
    if data != expected:
        raise ThreadTransitionConflict(
            "blocked source projection conflicts with admission authority"
        )


def _wake_token_for_release(
    *,
    user_id: str,
    thread_id: str,
    admission: Mapping[str, Any],
    released_blocker: Mapping[str, Any],
    wake_generation: int,
) -> str:
    return canonical_json_hash(
        {
            "hashKind": _WAKE_TOKEN_HASH_KIND,
            "userId": user_id,
            "threadId": thread_id,
            "canonicalSourceId": admission["canonicalSourceId"],
            "admissionHash": admission["admissionHash"],
            "releasedHeadHash": released_blocker["headHash"],
            "releasedGeneration": released_blocker["generation"],
            "wakeGeneration": wake_generation,
        }
    )


def _build_classification_snapshot_material(
    *,
    canonical_source_id: str,
    classification_input_hash: str,
    model_request_key: str | None,
    complete_proposal: Mapping[str, Any],
    proposal_evidence: Mapping[str, Any] | None,
    deterministic_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    normalized_proposal = _normalize_complete_proposal(complete_proposal)
    copied_proposal_evidence = None
    if proposal_evidence is not None:
        copied_proposal_evidence = _copy_exact_json_mapping(
            proposal_evidence,
            field_name="proposal evidence",
        )
    copied_deterministic_evidence = None
    if deterministic_evidence is not None:
        copied_deterministic_evidence = _copy_exact_json_mapping(
            deterministic_evidence,
            field_name="deterministic evidence",
        )
    if (copied_proposal_evidence is None) == (copied_deterministic_evidence is None):
        raise SourceCoordinatorConfigError(
            "classification snapshot requires exactly one evidence lane"
        )
    if copied_deterministic_evidence is not None and model_request_key is not None:
        raise SourceCoordinatorConfigError(
            "deterministic classification cannot retain a model request key"
        )
    if copied_proposal_evidence is not None:
        _validate_model_request_key(model_request_key)

    transition_candidates, ordinary_obligations, selection_snapshot = (
        _derive_classification_selection(
            canonical_source_id=canonical_source_id,
            complete_proposal=normalized_proposal,
            deterministic_hard_optout=(
                copied_deterministic_evidence is not None
                and copied_deterministic_evidence.get("evidenceKind")
                not in _LOCAL_SOURCE_POLICY_EVIDENCE_KINDS
            ),
        )
    )
    complete_proposal_hash = canonical_json_hash(normalized_proposal)
    proposal_evidence_hash = (
        canonical_json_hash(copied_proposal_evidence)
        if copied_proposal_evidence is not None
        else None
    )
    deterministic_evidence_hash = (
        canonical_json_hash(copied_deterministic_evidence)
        if copied_deterministic_evidence is not None
        else None
    )
    selection_hash = canonical_json_hash(selection_snapshot)
    immutable_payload = {
        "schemaVersion": _CLASSIFICATION_SNAPSHOT_SCHEMA_VERSION,
        "hashKind": _CLASSIFICATION_SNAPSHOT_HASH_KIND,
        "canonicalSourceId": canonical_source_id,
        "classificationInputSchemaVersion": _CLASSIFICATION_INPUT_SCHEMA_VERSION,
        "classificationInputHash": classification_input_hash,
        "modelRequestKey": model_request_key,
        "completeProposalSnapshot": normalized_proposal,
        "completeProposalHash": complete_proposal_hash,
        "transitionCandidates": transition_candidates,
        "ordinaryObligations": ordinary_obligations,
        "selectionSnapshot": selection_snapshot,
        "selectionHash": selection_hash,
        "proposalEvidence": copied_proposal_evidence,
        "proposalEvidenceHash": proposal_evidence_hash,
        "deterministicEvidence": copied_deterministic_evidence,
        "deterministicEvidenceHash": deterministic_evidence_hash,
    }
    snapshot_immutable_hash = canonical_json_hash(immutable_payload)
    bounded_payload = {
        **immutable_payload,
        "snapshotImmutableHash": snapshot_immutable_hash,
    }
    if len(_canonical_json_bytes(bounded_payload)) > MAX_CLASSIFICATION_SNAPSHOT_BYTES:
        raise ClassificationSnapshotTooLarge(
            "classification snapshot exceeds canonical byte limit"
        )
    return {
        "completeProposalSnapshot": normalized_proposal,
        "completeProposalHash": complete_proposal_hash,
        "transitionCandidates": transition_candidates,
        "ordinaryObligations": ordinary_obligations,
        "selectionSnapshot": selection_snapshot,
        "selectionHash": selection_hash,
        "snapshotImmutableHash": snapshot_immutable_hash,
        "proposalEvidence": copied_proposal_evidence,
        "proposalEvidenceHash": proposal_evidence_hash,
        "deterministicEvidence": copied_deterministic_evidence,
        "deterministicEvidenceHash": deterministic_evidence_hash,
    }


def _empty_classification_snapshot_fields() -> dict[str, Any]:
    return {field: None for field in _CLASSIFICATION_SNAPSHOT_FIELDS}


def _empty_retained_terminal_classification_fields() -> dict[str, Any]:
    return {field: None for field in _RETAINED_TERMINAL_CLASSIFICATION_FIELDS}


def _retained_terminal_hash_value(
    value: Any,
    *,
    active_containers: set[int],
) -> list[Any]:
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", value]
    if type(value) is float:
        if not math.isfinite(value):
            raise SourceCoordinatorConfigError(
                "retained terminal record contains a non-finite number"
            )
        return ["float", value]
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise SourceCoordinatorConfigError(
                "retained terminal record contains invalid UTF-8"
            ) from exc
        return ["string", value]
    if _is_aware_datetime(value):
        timestamp = _canonical_datetime_token(
            value,
            field_name="retained terminal timestamp",
        )
        nanosecond = getattr(value, "nanosecond", None)
        if type(nanosecond) is not int:
            nanosecond = None
        return ["datetime", timestamp, nanosecond]
    if type(value) not in {dict, list}:
        raise SourceCoordinatorConfigError(
            "retained terminal record contains an unsupported value"
        )

    container_id = id(value)
    if container_id in active_containers:
        raise SourceCoordinatorConfigError(
            "retained terminal record contains a cycle"
        )
    active_containers.add(container_id)
    try:
        if type(value) is list:
            return [
                "list",
                [
                    _retained_terminal_hash_value(
                        item,
                        active_containers=active_containers,
                    )
                    for item in value
                ],
            ]
        if any(type(key) is not str for key in value):
            raise SourceCoordinatorConfigError(
                "retained terminal record keys must be strings"
            )
        items = []
        for key in sorted(value):
            items.append(
                [
                    key,
                    _retained_terminal_hash_value(
                        value[key],
                        active_containers=active_containers,
                    ),
                ]
            )
        return ["mapping", items]
    except (RecursionError, TypeError) as exc:
        raise SourceCoordinatorConfigError(
            "retained terminal record cannot be canonically hashed"
        ) from exc
    finally:
        active_containers.remove(container_id)


def _retained_terminal_record_hash(record: dict[str, Any]) -> str:
    return canonical_json_hash(
        {
            "hashKind": _RETAINED_TERMINAL_RECORD_HASH_KIND,
            "record": _retained_terminal_hash_value(
                record,
                active_containers=set(),
            ),
        }
    )


def _verified_retained_terminal_evidence_from_loader(
    loader_result: Any,
    *,
    user_id: str,
    canonical_source_id: str,
    thread_id: str,
    thread_client_id: Any,
    supplied_aliases: Sequence[SourceAlias],
    identity_descriptors: Sequence[Mapping[str, str]],
) -> _VerifiedRetainedTerminalEvidence | None:
    required_fields = {
        "kind",
        "saga",
        "settlement",
        "exactSourceConfirmed",
    }
    if (
        type(loader_result) is not dict
        or any(type(key) is not str for key in loader_result)
        or set(loader_result) != required_fields
    ):
        raise SourceCoordinatorConfigError(
            "retained terminal authority loader returned a malformed disposition"
        )
    kind = loader_result.get("kind")
    if type(kind) is not str or kind not in {"ordinary", "active", "settled"}:
        raise SourceCoordinatorConfigError(
            "retained terminal authority loader returned an unsupported kind"
        )
    exact_source_confirmed = loader_result.get("exactSourceConfirmed")
    saga = loader_result.get("saga")
    settlement = loader_result.get("settlement")
    if kind == "ordinary":
        if (
            exact_source_confirmed is not False
            or saga is not None
            or settlement is not None
        ):
            raise SourceCoordinatorConfigError(
                "ordinary retained terminal disposition is malformed"
            )
        return None
    if exact_source_confirmed is not True:
        raise SourceCoordinatorConfigError(
            "retained terminal authority is not exact-source confirmed"
        )
    record = saga if kind == "active" else settlement
    other_record = settlement if kind == "active" else saga
    if type(record) is not dict or other_record is not None:
        raise SourceCoordinatorConfigError(
            "retained terminal authority selected record is malformed"
        )
    _validate_document_id(
        thread_client_id,
        field_name="retained terminal thread client id",
    )
    if kind == "active":
        record_client_id = record.get("clientId")
    else:
        saga_snapshot = record.get("sagaSnapshot")
        if type(saga_snapshot) is not dict:
            raise SourceCoordinatorConfigError(
                "settled retained terminal authority saga snapshot is malformed"
            )
        record_client_id = saga_snapshot.get("clientId")
    _validate_document_id(
        record_client_id,
        field_name="retained terminal record client id",
    )
    if record_client_id != thread_client_id:
        raise RetainedTerminalAuthorityConflict(
            "retained terminal authority conflicts with thread client binding"
        )
    record_hash = _retained_terminal_record_hash(record)

    record_aliases = []
    raw_aliases = {}
    for field_name, alias_type in (
        ("sourceGraphMessageId", "graph"),
        ("sourceInternetMessageId", "internet_message_id"),
    ):
        raw_value = record.get(field_name)
        if raw_value is None:
            continue
        if type(raw_value) is not str or not raw_value.strip():
            raise SourceCoordinatorConfigError(
                "retained terminal authority has a malformed typed source alias"
            )
        normalized = normalize_source_alias(alias_type, raw_value)
        alias = SourceAlias(
            alias_type=alias_type,
            value=normalized.value,
            key=source_alias_key(user_id, normalized),
        )
        record_aliases.append(alias)
        raw_aliases[alias_type] = raw_value.strip()
    if not record_aliases:
        raise SourceCoordinatorConfigError(
            "retained terminal authority lacks a typed source alias"
        )

    supplied_by_type = {alias.alias_type: alias for alias in supplied_aliases}
    record_by_type = {alias.alias_type: alias for alias in record_aliases}
    if any(
        record_by_type.get(alias_type) != alias
        for alias_type, alias in supplied_by_type.items()
    ):
        raise RetainedTerminalAuthorityConflict(
            "retained terminal authority conflicts with supplied source aliases"
        )
    descriptors_by_key = {
        descriptor["sourceAliasKey"]: dict(descriptor)
        for descriptor in identity_descriptors
    }
    for alias in record_aliases:
        if descriptors_by_key.get(alias.key) != _alias_descriptor(alias):
            raise RetainedTerminalAuthorityConflict(
                "retained terminal authority conflicts with source identity"
            )

    source_message_key = record.get("sourceMessageKey")
    if type(source_message_key) is not str or not source_message_key.strip():
        raise SourceCoordinatorConfigError(
            "retained terminal authority source-message key is malformed"
        )
    source_message_key = source_message_key.strip()
    expected_source_message_key = raw_aliases.get(
        "internet_message_id",
        raw_aliases.get("graph"),
    )
    if source_message_key != expected_source_message_key:
        raise RetainedTerminalAuthorityConflict(
            "retained terminal source-message key conflicts with typed aliases"
        )

    saga_key = record.get("sagaKey")
    if kind == "active" and (
        type(saga_key) is not str or not saga_key.strip()
    ):
        raise SourceCoordinatorConfigError(
            "active retained terminal authority lacks a saga key"
        )
    if saga_key is not None and (
        type(saga_key) is not str or not saga_key.strip()
    ):
        raise SourceCoordinatorConfigError(
            "retained terminal authority saga key is malformed"
        )
    saga_key = saga_key.strip() if type(saga_key) is str else None

    immutable_field = "immutableHash" if kind == "active" else "projectionHash"
    immutable_hash = record.get(immutable_field)
    if not _is_sha256(immutable_hash):
        raise SourceCoordinatorConfigError(
            "retained terminal immutable authority hash is malformed"
        )
    sorted_aliases = tuple(sorted(record_aliases, key=lambda alias: alias.key))
    binding_hash = canonical_json_hash(
        {
            "hashKind": _RETAINED_TERMINAL_BINDING_HASH_KIND,
            "schemaVersion": 1,
            "canonicalSourceId": canonical_source_id,
            "threadId": thread_id,
            "terminalKind": kind,
            "sourceAliases": [
                _alias_descriptor(alias) for alias in sorted_aliases
            ],
            "sourceMessageKey": source_message_key,
            "sagaKey": saga_key,
            "immutableAuthorityHash": immutable_hash,
            "validatedRecordHash": record_hash,
        }
    )
    verified = object.__new__(_VerifiedRetainedTerminalEvidence)
    object.__setattr__(verified, "terminal_kind", kind)
    object.__setattr__(verified, "immutable_hash", immutable_hash)
    object.__setattr__(verified, "record_hash", record_hash)
    object.__setattr__(verified, "binding_hash", binding_hash)
    object.__setattr__(verified, "aliases", sorted_aliases)
    return verified


def _classification_claim_from_data(data: Mapping[str, Any]) -> ClassificationClaim:
    return ClassificationClaim(
        canonical_source_id=data["canonicalSourceId"],
        classification_epoch=data["classificationEpoch"],
        classification_claim_id=data["classificationClaimId"],
        lease_expires_at=data["leaseExpiresAt"],
    )


def _classification_snapshot_from_data(
    data: Mapping[str, Any],
) -> ClassificationSnapshot:
    return ClassificationSnapshot(
        canonical_source_id=data["canonicalSourceId"],
        complete_proposal=_freeze_json(deepcopy(data["completeProposalSnapshot"])),
        complete_proposal_hash=data["completeProposalHash"],
        selection_snapshot=_freeze_json(deepcopy(data["selectionSnapshot"])),
        selection_hash=data["selectionHash"],
        snapshot_immutable_hash=data["snapshotImmutableHash"],
    )


def _validate_classification_claim_coordinates(
    *,
    classification_epoch: Any,
    classification_claim_id: Any,
) -> tuple[int, str]:
    epoch = _validate_positive_integer(
        classification_epoch,
        field_name="classification epoch",
    )
    _validate_document_id(
        classification_claim_id,
        field_name="classification claim id",
    )
    return epoch, classification_claim_id


def _validate_classification_document(
    data: Mapping[str, Any],
    *,
    canonical_source_id: str,
) -> None:
    if (
        type(data) is not dict
        or set(data) != _CLASSIFICATION_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _CLASSIFICATION_SCHEMA_VERSION,
        )
        or data.get("canonicalSourceId") != canonical_source_id
        or not _is_exact_schema_version(
            data.get("classificationInputSchemaVersion"),
            _CLASSIFICATION_INPUT_SCHEMA_VERSION,
        )
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
    ):
        raise SourceCoordinatorAmbiguous("source classification is malformed")

    state = data.get("classificationState")
    model_state = data.get("modelRequestState")
    snapshot_values = {field: data.get(field) for field in _CLASSIFICATION_SNAPSHOT_FIELDS}
    retained_terminal_values = {
        field: data.get(field)
        for field in _RETAINED_TERMINAL_CLASSIFICATION_FIELDS
    }
    if state == "legacy_terminal_quarantined":
        if (
            data.get("classificationEpoch") != 0
            or type(data.get("classificationEpoch")) is not int
            or data.get("classificationClaimId") is not None
            or data.get("leaseExpiresAt") is not None
            or model_state != "not_applicable"
            or data.get("classificationInputHash") is not None
            or data.get("modelRequestKey") is not None
            or data.get("requestStartFence") is not None
            or any(value is not None for value in snapshot_values.values())
            or data.get("retainedTerminalKind") not in {"active", "settled"}
            or any(
                not _is_sha256(data.get(field))
                for field in (
                    "retainedTerminalImmutableHash",
                    "retainedTerminalRecordHash",
                    "retainedTerminalBindingHash",
                )
            )
        ):
            raise SourceCoordinatorAmbiguous(
                "retained terminal classification quarantine is malformed"
            )
        return
    try:
        _validate_classification_claim_coordinates(
            classification_epoch=data.get("classificationEpoch"),
            classification_claim_id=data.get("classificationClaimId"),
        )
    except SourceCoordinatorConfigError as exc:
        raise SourceCoordinatorAmbiguous("source classification is malformed") from exc
    if not _is_aware_datetime(data.get("leaseExpiresAt")):
        raise SourceCoordinatorAmbiguous("source classification is malformed")
    if any(value is not None for value in retained_terminal_values.values()):
        raise SourceCoordinatorAmbiguous(
            "classification retains unsupported terminal authority"
        )
    if state == "claimed":
        if (
            model_state != "not_started"
            or data.get("classificationInputHash") is not None
            or data.get("modelRequestKey") is not None
            or data.get("requestStartFence") is not None
            or any(value is not None for value in snapshot_values.values())
        ):
            raise SourceCoordinatorAmbiguous("claimed classification is malformed")
        return

    if state in {"request_started", "classification_request_ambiguous"}:
        expected_model_state = (
            "started" if state == "request_started" else "ambiguous"
        )
        if (
            model_state != expected_model_state
            or not _is_sha256(data.get("classificationInputHash"))
            or type(data.get("requestStartFence")) is not str
            or not data.get("requestStartFence")
            or any(value is not None for value in snapshot_values.values())
        ):
            raise SourceCoordinatorAmbiguous(
                "started classification request is malformed"
            )
        try:
            _validate_model_request_key(data.get("modelRequestKey"))
            _validate_document_id(
                data.get("requestStartFence"),
                field_name="request start fence",
            )
        except SourceCoordinatorConfigError as exc:
            raise SourceCoordinatorAmbiguous(
                "started classification request is malformed"
            ) from exc
        return

    if state != "snapshot_ready" or model_state not in {"captured", "not_applicable"}:
        raise SourceCoordinatorAmbiguous("source classification state is unsupported")
    if (
        not _is_sha256(data.get("classificationInputHash"))
        or not _is_aware_datetime(data.get("snapshotPersistedAt"))
    ):
        raise SourceCoordinatorAmbiguous("classification snapshot is malformed")
    if model_state == "captured":
        if type(data.get("requestStartFence")) is not str:
            raise SourceCoordinatorAmbiguous("classification snapshot is malformed")
        try:
            _validate_model_request_key(data.get("modelRequestKey"))
            _validate_document_id(
                data.get("requestStartFence"),
                field_name="request start fence",
            )
        except SourceCoordinatorConfigError as exc:
            raise SourceCoordinatorAmbiguous("classification snapshot is malformed") from exc
        proposal_evidence = data.get("proposalEvidence")
        deterministic_evidence = None
    else:
        if data.get("modelRequestKey") is not None or data.get("requestStartFence") is not None:
            raise SourceCoordinatorAmbiguous("deterministic snapshot is malformed")
        proposal_evidence = None
        deterministic_evidence = data.get("deterministicEvidence")
    try:
        expected_material = _build_classification_snapshot_material(
            canonical_source_id=canonical_source_id,
            classification_input_hash=data["classificationInputHash"],
            model_request_key=data.get("modelRequestKey"),
            complete_proposal=data.get("completeProposalSnapshot"),
            proposal_evidence=proposal_evidence,
            deterministic_evidence=deterministic_evidence,
        )
    except SourceCoordinatorError as exc:
        raise SourceCoordinatorAmbiguous("classification snapshot is malformed") from exc
    if any(data.get(field) != value for field, value in expected_material.items()):
        raise SourceCoordinatorAmbiguous("classification snapshot hashes conflict")


def _stable_model_request_key(
    *,
    canonical_source_id: str,
    classification_epoch: int,
    classification_claim_id: str,
    classification_input_hash: str,
) -> str:
    return canonical_json_hash(
        {
            "hashKind": _CLASSIFICATION_REQUEST_KEY_KIND,
            "canonicalSourceId": canonical_source_id,
            "classificationEpoch": classification_epoch,
            "classificationClaimId": classification_claim_id,
            "classificationInputHash": classification_input_hash,
        }
    )


def _deterministic_hard_optout_proposal(
    deterministic_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_hash = canonical_json_hash(deterministic_evidence)
    return {
        "schemaVersion": _CLASSIFICATION_SNAPSHOT_SCHEMA_VERSION,
        "transitionCandidates": [
            {
                "type": "contact_optout",
                "evidenceHash": evidence_hash,
            }
        ],
        "ordinaryObligations": [],
    }


def _build_source_admission_envelope(
    *,
    user_id: str,
    hydrated_message: Mapping[str, Any],
    evidence_kind: str,
) -> _SourceAdmissionEnvelope:
    if type(hydrated_message) is not dict:
        raise SourceCoordinatorConfigError(
            "hydrated message must be an exact mapping"
        )
    if (
        type(evidence_kind) is not str
        or evidence_kind not in _SOURCE_ADMISSION_EVIDENCE_KINDS
    ):
        raise SourceCoordinatorConfigError(
            "source admission evidence kind is unsupported"
        )
    evidence_hash = canonical_json_hash(
        {
            "schemaVersion": _SOURCE_IDENTITY_SCHEMA_VERSION,
            "evidenceKind": evidence_kind,
            "hydratedMessage": hydrated_message,
        }
    )
    aliases = []
    for field, alias_type in (
        ("id", "graph"),
        ("internetMessageId", "internet_message_id"),
    ):
        if field not in hydrated_message:
            continue
        normalized = normalize_source_alias(alias_type, hydrated_message[field])
        aliases.append(
            SourceAlias(
                alias_type=normalized.alias_type,
                value=normalized.value,
                key=source_alias_key(user_id, normalized),
            )
        )
    aliases.sort(key=lambda alias: alias.key)
    if len(aliases) > MAX_SOURCE_ALIASES:
        raise SourceAliasLimitExceeded("source alias limit exceeded")
    return _SourceAdmissionEnvelope(
        aliases=tuple(aliases),
        evidence_kind=evidence_kind,
        evidence_hash=evidence_hash,
    )


def _validate_alias_projection(
    data: Mapping[str, Any],
    *,
    descriptor: Mapping[str, str],
    canonical_source_id: str,
) -> None:
    if set(data) != _SOURCE_ALIAS_PROJECTION_FIELDS:
        raise SourceCoordinatorAmbiguous(
            "source alias projection schema is malformed"
        )
    if not _is_exact_schema_version(
        data.get("schemaVersion"),
        _SOURCE_IDENTITY_SCHEMA_VERSION,
    ):
        raise SourceAliasConflict(
            "source alias projection conflicts with authority"
        )
    expected = {
        "schemaVersion": _SOURCE_IDENTITY_SCHEMA_VERSION,
        **descriptor,
        "canonicalSourceId": canonical_source_id,
    }
    if any(data.get(key) != value for key, value in expected.items()):
        raise SourceAliasConflict("source alias projection conflicts with authority")
    if not _is_aware_datetime(data.get("createdAt")):
        raise SourceCoordinatorAmbiguous("source alias projection is malformed")


def _validated_identity_descriptors(
    data: Mapping[str, Any],
    *,
    canonical_source_id: str,
) -> list[dict[str, str]]:
    if (
        set(data) != _SOURCE_IDENTITY_FIELDS
        or not _is_exact_schema_version(
            data.get("schemaVersion"),
            _SOURCE_IDENTITY_SCHEMA_VERSION,
        )
        or data.get("canonicalSourceId") != canonical_source_id
        or not _is_sha256(data.get("creationHash"))
        or data.get("lifecycleState") != "pending"
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
    ):
        raise SourceCoordinatorAmbiguous("source identity is malformed")
    stored_thread_id = data.get("threadId")
    if stored_thread_id is not None:
        try:
            _validate_document_id(stored_thread_id, field_name="thread id")
        except SourceCoordinatorConfigError as exc:
            raise SourceCoordinatorAmbiguous(
                "source identity thread binding is malformed"
            ) from exc

    descriptors = data.get("verifiedAliases")
    if type(descriptors) is not list or not descriptors:
        raise SourceCoordinatorAmbiguous("source identity alias set is malformed")
    validated = []
    seen = set()
    for descriptor in descriptors:
        if type(descriptor) is not dict or set(descriptor) != {
            "sourceAliasKey",
            "aliasType",
            "normalizedValueHash",
        }:
            raise SourceCoordinatorAmbiguous("source identity alias set is malformed")
        alias_key = descriptor.get("sourceAliasKey")
        alias_type = descriptor.get("aliasType")
        value_hash = descriptor.get("normalizedValueHash")
        if (
            not _is_sha256(alias_key)
            or type(alias_type) is not str
            or alias_type not in _SOURCE_ALIAS_TYPES
            or not _is_sha256(value_hash)
            or alias_key in seen
        ):
            raise SourceCoordinatorAmbiguous("source identity alias set is malformed")
        seen.add(alias_key)
        validated.append(dict(descriptor))
    if len(validated) > MAX_SOURCE_ALIASES or validated != sorted(
        validated, key=lambda item: item["sourceAliasKey"]
    ):
        raise SourceCoordinatorAmbiguous("source identity alias set is malformed")
    return validated


class SourceCoordinator:
    def __init__(
        self,
        firestore_client,
        *,
        uuid_factory,
        now_factory,
        hard_optout_verifier=None,
        local_source_policy_verifier=None,
        retained_terminal_authority_loader=None,
    ):
        if (
            firestore_client is None
            or not callable(uuid_factory)
            or not callable(now_factory)
            or (
                hard_optout_verifier is not None
                and not callable(hard_optout_verifier)
            )
            or (
                local_source_policy_verifier is not None
                and not callable(local_source_policy_verifier)
            )
            or (
                retained_terminal_authority_loader is not None
                and not callable(retained_terminal_authority_loader)
            )
        ):
            raise SourceCoordinatorConfigError(
                "source coordinator dependencies are invalid"
            )
        self._firestore = firestore_client
        self._uuid_factory = uuid_factory
        self._now_factory = now_factory
        self._hard_optout_verifier = hard_optout_verifier
        self._local_source_policy_verifier = local_source_policy_verifier
        self._retained_terminal_authority_loader = (
            retained_terminal_authority_loader
        )

    def _classification_refs(self, *, user_id: str, canonical_source_id: str):
        user_ref = self._firestore.collection("users").document(user_id)
        return (
            user_ref.collection("sourceIdentities").document(canonical_source_id),
            user_ref.collection("sourceClassifications").document(
                canonical_source_id
            ),
        )

    @staticmethod
    def _require_source_identity_snapshot(
        snapshot: Any,
        *,
        canonical_source_id: str,
    ) -> Mapping[str, Any]:
        identity_data = _snapshot_data(snapshot)
        if identity_data is None:
            raise SourceIdentityMissing(
                "classification requires an admitted source identity"
            )
        _validated_identity_descriptors(
            identity_data,
            canonical_source_id=canonical_source_id,
        )
        return identity_data

    def _current_time(self) -> datetime:
        now = self._now_factory()
        if not _is_aware_datetime(now):
            raise SourceCoordinatorConfigError(
                "now factory must return an aware datetime"
            )
        return now

    def _allocate_document_token(self, *, field_name: str) -> str:
        token = self._uuid_factory()
        _validate_document_id(token, field_name=field_name)
        return token

    def _run_classification_transaction(self, prepare):
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "classification transaction is unavailable"
            ) from transaction_error
        prepared_plan = None

        @transactional
        def run_once(active_transaction):
            nonlocal prepared_plan
            prepared_plan = prepare(active_transaction)
            return prepared_plan.result

        try:
            result = run_once(transaction)
        except Exception as transaction_error:
            if prepared_plan is None:
                if isinstance(transaction_error, SourceCoordinatorError):
                    raise
                raise SourceCoordinatorAmbiguous(
                    "classification transaction failed before commit"
                ) from transaction_error
            try:
                identity_readback = self._require_source_identity_snapshot(
                    prepared_plan.identity_ref.get(),
                    canonical_source_id=prepared_plan.classification_ref.id,
                )
                if identity_readback != prepared_plan.identity_data:
                    raise SourceCoordinatorAmbiguous(
                        "source identity changed during classification commit"
                    )
                readback = _snapshot_data(prepared_plan.classification_ref.get())
                if readback is not None:
                    _validate_classification_document(
                        readback,
                        canonical_source_id=prepared_plan.classification_ref.id,
                    )
            except Exception as readback_error:
                raise prepared_plan.ambiguous_error_type(
                    "classification commit outcome is unreadable"
                ) from readback_error
            if readback == prepared_plan.expected_data:
                result = prepared_plan.result
            elif readback == prepared_plan.before_data:
                raise SourceCoordinatorRetryable(
                    "classification commit was not applied"
                ) from transaction_error
            else:
                raise prepared_plan.ambiguous_error_type(
                    "classification commit outcome is ambiguous"
                ) from transaction_error
        if isinstance(result, _DeferredClassificationError):
            raise result.error
        return result

    def _run_authority_create_transaction(self, prepare, *, authority_name):
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                f"{authority_name} transaction is unavailable"
            ) from transaction_error
        prepared_plan = None

        @transactional
        def run_once(active_transaction):
            nonlocal prepared_plan
            prepared_plan = prepare(active_transaction)
            return prepared_plan.result

        try:
            return run_once(transaction)
        except Exception as transaction_error:
            if prepared_plan is None:
                if isinstance(transaction_error, SourceCoordinatorError):
                    raise
                raise SourceCoordinatorAmbiguous(
                    f"{authority_name} transaction failed before commit"
                ) from transaction_error
            try:
                for reference, expected in prepared_plan.prerequisites:
                    if _snapshot_data(reference.get()) != expected:
                        raise SourceCoordinatorAmbiguous(
                            f"{authority_name} prerequisite changed during commit"
                        )
                readback = _snapshot_data(prepared_plan.target_ref.get())
            except Exception as readback_error:
                raise prepared_plan.ambiguous_error_type(
                    f"{authority_name} commit outcome is unreadable"
                ) from readback_error
            if readback == prepared_plan.expected_data:
                return prepared_plan.result
            if readback == prepared_plan.before_data:
                raise SourceCoordinatorRetryable(
                    f"{authority_name} commit was not applied"
                ) from transaction_error
            raise prepared_plan.ambiguous_error_type(
                f"{authority_name} commit outcome is ambiguous"
            ) from transaction_error

    def _run_multi_document_transaction(self, prepare, *, authority_name):
        try:
            transaction = self._firestore.transaction(max_attempts=3)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                f"{authority_name} transaction is unavailable"
            ) from transaction_error
        prepared_plan = None

        @transactional
        def run_once(active_transaction):
            nonlocal prepared_plan
            prepared_plan = None
            prepared_plan = prepare(active_transaction)
            return prepared_plan.result

        try:
            return run_once(transaction)
        except Exception as transaction_error:
            if prepared_plan is None:
                if isinstance(transaction_error, SourceCoordinatorError):
                    raise
                raise SourceCoordinatorAmbiguous(
                    f"{authority_name} transaction failed before commit"
                ) from transaction_error
            try:
                prerequisite_matches = all(
                    _snapshot_data(reference.get()) == expected
                    for reference, expected in prepared_plan.prerequisites
                )
                mutation_readbacks = [
                    (_snapshot_data(reference.get()), before, expected)
                    for reference, before, expected in prepared_plan.mutations
                ]
            except Exception as readback_error:
                raise prepared_plan.ambiguous_error_type(
                    f"{authority_name} commit outcome is unreadable"
                ) from readback_error
            if prerequisite_matches and all(
                readback == expected
                for readback, _before, expected in mutation_readbacks
            ):
                return prepared_plan.result
            if prerequisite_matches and all(
                readback == before
                for readback, before, _expected in mutation_readbacks
            ):
                raise SourceCoordinatorRetryable(
                    f"{authority_name} commit was not applied"
                ) from transaction_error
            raise prepared_plan.ambiguous_error_type(
                f"{authority_name} commit outcome is ambiguous"
            ) from transaction_error

    @staticmethod
    def _stage_document_mutations(transaction, mutations) -> None:
        for reference, before, expected in mutations:
            if expected is None:
                transaction.delete(reference)
            elif before is None:
                transaction.create(reference, deepcopy(expected))
            else:
                transaction.set(reference, deepcopy(expected))

    @staticmethod
    def _source_sync_ref(user_ref):
        return user_ref.collection("sync").document("inbox")

    def _read_source_authority_revision(
        self,
        *,
        transaction,
        user_ref,
        allow_missing: bool,
    ) -> tuple[Any, Mapping[str, Any] | None, int | None]:
        sync_ref = self._source_sync_ref(user_ref)
        sync_data = _snapshot_data(sync_ref.get(transaction=transaction))
        revision = _source_authority_revision_from_sync(
            sync_data,
            allow_missing=allow_missing,
        )
        return sync_ref, sync_data, revision

    def _require_source_authority_revision(
        self,
        *,
        transaction,
        user_ref,
        expected_revision: int | None,
    ) -> tuple[Any, Mapping[str, Any] | None]:
        sync_ref, sync_data, revision = self._read_source_authority_revision(
            transaction=transaction,
            user_ref=user_ref,
            allow_missing=expected_revision is None,
        )
        if revision != expected_revision:
            raise SourceCoordinatorRetryable(
                "source authority changed during cursor audit"
            )
        return sync_ref, sync_data

    def _require_empty_source_authority_inventory(
        self,
        *,
        transaction,
        user_ref,
    ) -> None:
        for collection_name in sorted(_SOURCE_AUTHORITY_INVENTORY_COLLECTIONS):
            query = user_ref.collection(collection_name).order_by(
                "__name__"
            ).limit(1)
            if list(transaction.get(query)):
                raise SourceCoordinatorAmbiguous(
                    "source authority exists without an inventory revision"
                )

    def _next_source_authority_revision_mutation(
        self,
        *,
        transaction,
        user_ref,
        allow_first_identity: bool,
    ) -> tuple[Any, Mapping[str, Any] | None, Mapping[str, Any]]:
        sync_ref, sync_before, revision = self._read_source_authority_revision(
            transaction=transaction,
            user_ref=user_ref,
            allow_missing=allow_first_identity,
        )
        if revision is None:
            if not allow_first_identity:
                raise SourceCoordinatorAmbiguous(
                    "source authority revision is missing"
                )
            self._require_empty_source_authority_inventory(
                transaction=transaction,
                user_ref=user_ref,
            )
            next_revision = 1
        else:
            next_revision = revision + 1
        sync_expected = deepcopy(sync_before) if sync_before is not None else {}
        sync_expected.update(
            {
                _SOURCE_AUTHORITY_REVISION_FIELD: next_revision,
                "updatedAt": self._current_time(),
            }
        )
        return sync_ref, sync_before, sync_expected

    def _stage_source_authority_mutations(
        self,
        transaction,
        *,
        user_ref,
        mutations,
    ) -> tuple[
        tuple[Any, Mapping[str, Any] | None, Mapping[str, Any] | None], ...
    ]:
        staged = tuple(mutations)

        def affects_audit_verdict(reference, before, expected) -> bool:
            path_parts = reference.path.split("/")
            if len(path_parts) < 2:
                return False
            collection_name = path_parts[-2]
            if collection_name in _SOURCE_AUTHORITY_VERDICT_COLLECTIONS:
                return True
            if collection_name != "processedMessages":
                return False
            return any(
                type(value) is dict
                and any(
                    field in value
                    for field in _SOURCE_PROCESSED_OWNERSHIP_FIELDS
                )
                for value in (before, expected)
            )

        verdict_change = any(
            affects_audit_verdict(reference, before, expected)
            for reference, before, expected in staged
            if before != expected
        )
        if verdict_change:
            revision_mutation = self._next_source_authority_revision_mutation(
                transaction=transaction,
                user_ref=user_ref,
                allow_first_identity=False,
            )
            staged = (*staged, revision_mutation)
        self._stage_document_mutations(transaction, staged)
        return staged

    def _read_source_authority_bundle(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
        expected_thread_id: str | None = None,
    ) -> tuple[
        tuple[Any, Mapping[str, Any]],
        tuple[Any, Mapping[str, Any]],
        tuple[Any, Mapping[str, Any]],
        tuple[Any, Mapping[str, Any]],
    ]:
        identity_ref = user_ref.collection("sourceIdentities").document(
            canonical_source_id
        )
        classification_ref = user_ref.collection("sourceClassifications").document(
            canonical_source_id
        )
        owner_ref = user_ref.collection("sourceTransitionOwners").document(
            canonical_source_id
        )
        ledger_ref = user_ref.collection("sourceWorkLedgers").document(
            canonical_source_id
        )
        identity_data = self._require_source_identity_snapshot(
            identity_ref.get(transaction=transaction),
            canonical_source_id=canonical_source_id,
        )
        thread_id = identity_data.get("threadId")
        if type(thread_id) is not str or not thread_id:
            raise ThreadTransitionConflict(
                "source authority requires a retained thread binding"
            )
        if expected_thread_id is not None and thread_id != expected_thread_id:
            raise ThreadTransitionConflict(
                "source authority conflicts with the requested thread"
            )
        classification_data = _snapshot_data(
            classification_ref.get(transaction=transaction)
        )
        if classification_data is None:
            raise ClassificationSnapshotNotReady(
                "thread authority requires a classification snapshot"
            )
        _validate_classification_document(
            classification_data,
            canonical_source_id=canonical_source_id,
        )
        if classification_data["classificationState"] != "snapshot_ready":
            raise ClassificationSnapshotNotReady(
                "thread authority requires snapshot_ready classification"
            )
        owner_data = _snapshot_data(owner_ref.get(transaction=transaction))
        if owner_data is None:
            raise TransitionOwnerConflict(
                "thread authority requires an explicit transition owner"
            )
        _validate_transition_owner_document(
            owner_data,
            canonical_source_id=canonical_source_id,
            classification_data=classification_data,
        )
        ledger_data = _snapshot_data(ledger_ref.get(transaction=transaction))
        if ledger_data is None:
            raise SourceWorkLedgerConflict(
                "thread authority requires a source work ledger"
            )
        _validate_source_work_ledger_document(
            ledger_data,
            canonical_source_id=canonical_source_id,
            classification_data=classification_data,
            owner_data=owner_data,
        )
        return (
            (identity_ref, identity_data),
            (classification_ref, classification_data),
            (owner_ref, owner_data),
            (ledger_ref, ledger_data),
        )

    def _query_pending_admissions(
        self,
        *,
        transaction,
        collection_ref,
        thread_id: str,
    ) -> list[tuple[Any, dict[str, Any]]]:
        query = collection_ref.where("threadId", "==", thread_id)
        snapshots = list(transaction.get(query))
        admissions = []
        for snapshot in snapshots:
            data = _snapshot_data(snapshot)
            if data is None:
                raise SourceCoordinatorAmbiguous(
                    "pending admission query returned an absent document"
                )
            _validate_pending_admission_document(
                data,
                canonical_source_id=snapshot.id,
                thread_id=thread_id,
            )
            admissions.append((snapshot.reference, data))
        admissions.sort(key=lambda item: item[1]["canonicalSourceId"])
        return admissions

    def _run_source_authority_audit_read(
        self,
        *,
        user_ref,
        expected_revision: int | None,
        read,
    ):
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "source authority audit transaction is unavailable"
            ) from transaction_error

        @transactional
        def read_once(active_transaction):
            self._require_source_authority_revision(
                transaction=active_transaction,
                user_ref=user_ref,
                expected_revision=expected_revision,
            )
            return read(active_transaction)

        try:
            return read_once(transaction)
        except SourceCoordinatorError:
            raise
        except Exception as read_error:
            raise SourceCoordinatorRetryable(
                "source authority audit read is unavailable"
            ) from read_error

    def _source_authority_query_pages(
        self,
        *,
        user_ref,
        expected_revision: int | None,
        query,
        page_size: int,
    ):
        cursor = None
        while True:
            def read_page(transaction):
                page_query = query.limit(page_size)
                if cursor is not None:
                    page_query = page_query.start_after(cursor)
                return tuple(transaction.get(page_query))

            page = self._run_source_authority_audit_read(
                user_ref=user_ref,
                expected_revision=expected_revision,
                read=read_page,
            )
            if not page:
                return
            if expected_revision is None:
                raise SourceCoordinatorAmbiguous(
                    "source authority exists without an inventory revision"
                )
            yield page
            if len(page) < page_size:
                return
            cursor = page[-1]

    def _audit_source_identity(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
    ) -> bool:
        identity_collection = user_ref.collection("sourceIdentities")
        admission_collection = user_ref.collection("inboundPendingAdmissions")
        identity_data = self._require_source_identity_snapshot(
            identity_collection.document(canonical_source_id).get(
                transaction=transaction
            ),
            canonical_source_id=canonical_source_id,
        )
        admission_data = _snapshot_data(
            admission_collection.document(canonical_source_id).get(
                transaction=transaction
            )
        )
        if admission_data is None:
            return True
        thread_id = admission_data.get("threadId")
        _validate_document_id(thread_id, field_name="thread id")
        _validate_pending_admission_document(
            admission_data,
            canonical_source_id=canonical_source_id,
            thread_id=thread_id,
        )
        bundle = self._read_source_authority_bundle(
            transaction=transaction,
            user_ref=user_ref,
            canonical_source_id=canonical_source_id,
            expected_thread_id=thread_id,
        )
        _validate_admission_authority_bindings(
            admission_data,
            identity_data=bundle[0][1],
            classification_data=bundle[1][1],
            owner_data=bundle[2][1],
            ledger_data=bundle[3][1],
        )
        if admission_data["admissionState"] != "settled":
            return True
        settlement_data = _snapshot_data(
            user_ref.collection("sourceSettlements")
            .document(canonical_source_id)
            .get(transaction=transaction)
        )
        if settlement_data is None:
            raise SourceSettlementConflict(
                "settled admission lacks canonical settlement authority"
            )
        aliases = _validated_identity_descriptors(
            identity_data,
            canonical_source_id=canonical_source_id,
        )
        _validate_source_settlement_document(
            settlement_data,
            canonical_source_id=canonical_source_id,
            identity_data=identity_data,
            classification_data=bundle[1][1],
            owner_data=bundle[2][1],
            ledger_data=bundle[3][1],
            current_aliases=aliases,
        )
        if bundle[2][1]["ownerKind"] != "none":
            self._audit_settled_owned_source_head(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
                admission_data=admission_data,
                owner_data=bundle[2][1],
                settlement_data=settlement_data,
            )
        for descriptor in aliases:
            alias_key = descriptor["sourceAliasKey"]
            alias_owner_data = _snapshot_data(
                user_ref.collection("sourceAliases")
                .document(alias_key)
                .get(transaction=transaction)
            )
            if alias_owner_data is None:
                raise SourceSettlementConflict(
                    "settled source alias lacks retained owner authority"
                )
            _validate_alias_projection(
                alias_owner_data,
                descriptor=descriptor,
                canonical_source_id=canonical_source_id,
            )
            processed_data = _snapshot_data(
                user_ref.collection("processedMessages")
                .document(alias_key)
                .get(transaction=transaction)
            )
            if processed_data is None:
                raise SourceSettlementConflict(
                    "settled source alias lacks processed projection"
                )
            _validate_processed_alias_projection(
                processed_data,
                descriptor=descriptor,
                canonical_source_id=canonical_source_id,
                settlement_data=settlement_data,
            )
        return False

    def _audit_settled_owned_source_head(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
        admission_data: Mapping[str, Any],
        owner_data: Mapping[str, Any],
        settlement_data: Mapping[str, Any],
    ) -> None:
        thread_id = admission_data["threadId"]
        head_data = _snapshot_data(
            user_ref.collection("threadTransitionHeads")
            .document(thread_id)
            .get(transaction=transaction)
        )
        if head_data is None:
            raise ThreadTransitionConflict(
                "settled owned source lacks a thread head outcome"
            )
        _validate_thread_head_document(head_data, thread_id=thread_id)
        retained_generation = admission_data["wakeGeneration"]
        minimum_generation = (
            retained_generation if retained_generation is not None else 1
        )
        if head_data["activeCanonicalSourceId"] == canonical_source_id:
            if (
                head_data["activeState"] not in {"active", "releasing"}
                or head_data["activeOwnerKind"] != owner_data["ownerKind"]
                or head_data["activeOwnerKey"] != owner_data["ownerKey"]
                or (
                    retained_generation is not None
                    and head_data["activeGeneration"] != retained_generation
                )
            ):
                raise ThreadTransitionConflict(
                    "settled owned source conflicts with its thread head outcome"
                )
            return
        if (
            head_data["activeGeneration"] < minimum_generation
            or head_data["threadHeadRevision"] < 2
            or head_data["updatedAt"] < settlement_data["settledAt"]
            or (
                head_data["activeState"] != "clear"
                and head_data["activeGeneration"] <= minimum_generation
            )
        ):
            raise ThreadTransitionConflict(
                "settled owned source lacks a valid post-release head outcome"
            )

    def _audit_source_admission(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
    ) -> None:
        admission_data = _snapshot_data(
            user_ref.collection("inboundPendingAdmissions")
            .document(canonical_source_id)
            .get(transaction=transaction)
        )
        if admission_data is None:
            raise SourceCoordinatorAmbiguous(
                "canonical source audit returned an absent admission"
            )
        thread_id = admission_data.get("threadId")
        _validate_document_id(thread_id, field_name="thread id")
        _validate_pending_admission_document(
            admission_data,
            canonical_source_id=canonical_source_id,
            thread_id=thread_id,
        )
        self._require_source_identity_snapshot(
            user_ref.collection("sourceIdentities")
            .document(canonical_source_id)
            .get(transaction=transaction),
            canonical_source_id=canonical_source_id,
        )

    @staticmethod
    def _require_support_document(
        *,
        transaction,
        user_ref,
        collection_name: str,
        document_id: str,
    ) -> Mapping[str, Any]:
        data = _snapshot_data(
            user_ref.collection(collection_name)
            .document(document_id)
            .get(transaction=transaction)
        )
        if data is None:
            raise SourceCoordinatorAmbiguous(
                "source authority support audit returned an absent document"
            )
        return data

    def _audit_source_alias_support(
        self,
        *,
        transaction,
        user_ref,
        alias_key: str,
    ) -> None:
        if not _is_sha256(alias_key):
            raise SourceAliasConflict("source alias document key is malformed")
        alias_data = self._require_support_document(
            transaction=transaction,
            user_ref=user_ref,
            collection_name="sourceAliases",
            document_id=alias_key,
        )
        canonical_source_id = alias_data.get("canonicalSourceId")
        try:
            _validate_document_id(
                canonical_source_id,
                field_name="canonical source id",
            )
        except SourceCoordinatorConfigError as source_error:
            raise SourceAliasConflict(
                "source alias owner is malformed"
            ) from source_error
        identity_data = self._require_source_identity_snapshot(
            user_ref.collection("sourceIdentities")
            .document(canonical_source_id)
            .get(transaction=transaction),
            canonical_source_id=canonical_source_id,
        )
        descriptors = _validated_identity_descriptors(
            identity_data,
            canonical_source_id=canonical_source_id,
        )
        descriptor = next(
            (
                candidate
                for candidate in descriptors
                if candidate["sourceAliasKey"] == alias_key
            ),
            None,
        )
        if descriptor is None:
            raise SourceAliasConflict(
                "source alias projection is absent from identity authority"
            )
        _validate_alias_projection(
            alias_data,
            descriptor=descriptor,
            canonical_source_id=canonical_source_id,
        )

    def _audit_source_classification_support(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
    ) -> Mapping[str, Any]:
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        self._require_source_identity_snapshot(
            user_ref.collection("sourceIdentities")
            .document(canonical_source_id)
            .get(transaction=transaction),
            canonical_source_id=canonical_source_id,
        )
        classification_data = self._require_support_document(
            transaction=transaction,
            user_ref=user_ref,
            collection_name="sourceClassifications",
            document_id=canonical_source_id,
        )
        _validate_classification_document(
            classification_data,
            canonical_source_id=canonical_source_id,
        )
        return classification_data

    def _audit_source_owner_support(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
    ) -> Mapping[str, Any]:
        classification_data = self._audit_source_classification_support(
            transaction=transaction,
            user_ref=user_ref,
            canonical_source_id=canonical_source_id,
        )
        if classification_data["classificationState"] != "snapshot_ready":
            raise TransitionOwnerConflict(
                "transition owner lacks snapshot-ready classification authority"
            )
        owner_data = self._require_support_document(
            transaction=transaction,
            user_ref=user_ref,
            collection_name="sourceTransitionOwners",
            document_id=canonical_source_id,
        )
        _validate_transition_owner_document(
            owner_data,
            canonical_source_id=canonical_source_id,
            classification_data=classification_data,
        )
        return owner_data

    def _audit_source_ledger_support(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
    ) -> tuple[
        tuple[Any, Mapping[str, Any]],
        tuple[Any, Mapping[str, Any]],
        tuple[Any, Mapping[str, Any]],
        tuple[Any, Mapping[str, Any]],
    ]:
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        return self._read_source_authority_bundle(
            transaction=transaction,
            user_ref=user_ref,
            canonical_source_id=canonical_source_id,
        )

    def _audit_source_deferred_work_support(
        self,
        *,
        transaction,
        user_ref,
        work_key: str,
    ) -> None:
        if not _is_sha256(work_key):
            raise DeferredWorkConflict(
                "source deferred-work document key is malformed"
            )
        deferred_data = self._require_support_document(
            transaction=transaction,
            user_ref=user_ref,
            collection_name="sourceDeferredWork",
            document_id=work_key,
        )
        canonical_source_id = deferred_data.get("canonicalSourceId")
        try:
            _validate_document_id(
                canonical_source_id,
                field_name="canonical source id",
            )
        except SourceCoordinatorConfigError as source_error:
            raise DeferredWorkConflict(
                "source deferred-work owner is malformed"
            ) from source_error
        bundle = self._audit_source_ledger_support(
            transaction=transaction,
            user_ref=user_ref,
            canonical_source_id=canonical_source_id,
        )
        ledger_data = bundle[3][1]
        try:
            _entry_index, entry = _find_bound_source_work_entry(
                ledger_data,
                ledger_hash=deferred_data.get("ledgerHash"),
                work_key=work_key,
                payload_hash=deferred_data.get("entryPayloadHash"),
            )
            expected_immutable = _source_deferred_work_immutable_material(
                canonical_source_id=canonical_source_id,
                ledger_hash=ledger_data["ledgerHash"],
                entry=entry,
            )
        except SourceCoordinatorError as binding_error:
            raise DeferredWorkConflict(
                "source deferred work lacks a bound ledger entry"
            ) from binding_error
        _validate_source_deferred_work_document(
            deferred_data,
            expected_immutable=expected_immutable,
        )
        if (
            entry["state"] != "delegated"
            or entry["resolutionEvidence"]["deferredBindingHash"]
            != deferred_data["bindingHash"]
        ):
            raise DeferredWorkConflict(
                "source deferred work lacks delegated ledger authority"
            )

    def _audit_blocked_source_support(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
    ) -> None:
        bundle = self._audit_source_ledger_support(
            transaction=transaction,
            user_ref=user_ref,
            canonical_source_id=canonical_source_id,
        )
        identity_data = bundle[0][1]
        admission_data = _snapshot_data(
            user_ref.collection("inboundPendingAdmissions")
            .document(canonical_source_id)
            .get(transaction=transaction)
        )
        if admission_data is None:
            raise ThreadTransitionConflict(
                "blocked source projection lacks an admission"
            )
        _validate_pending_admission_document(
            admission_data,
            canonical_source_id=canonical_source_id,
            thread_id=identity_data["threadId"],
        )
        _validate_admission_authority_bindings(
            admission_data,
            identity_data=identity_data,
            classification_data=bundle[1][1],
            owner_data=bundle[2][1],
            ledger_data=bundle[3][1],
        )
        projection_data = self._require_support_document(
            transaction=transaction,
            user_ref=user_ref,
            collection_name="blockedSources",
            document_id=canonical_source_id,
        )
        _validate_blocked_projection_document(
            projection_data,
            admission=admission_data,
        )

    def _audit_source_settlement_support(
        self,
        *,
        transaction,
        user_ref,
        canonical_source_id: str,
    ) -> None:
        bundle = self._audit_source_ledger_support(
            transaction=transaction,
            user_ref=user_ref,
            canonical_source_id=canonical_source_id,
        )
        identity_data = bundle[0][1]
        admission_data = _snapshot_data(
            user_ref.collection("inboundPendingAdmissions")
            .document(canonical_source_id)
            .get(transaction=transaction)
        )
        if admission_data is None:
            raise SourceSettlementConflict(
                "source settlement lacks an admission"
            )
        _validate_pending_admission_document(
            admission_data,
            canonical_source_id=canonical_source_id,
            thread_id=identity_data["threadId"],
        )
        _validate_admission_authority_bindings(
            admission_data,
            identity_data=identity_data,
            classification_data=bundle[1][1],
            owner_data=bundle[2][1],
            ledger_data=bundle[3][1],
        )
        if admission_data["admissionState"] != "settled":
            raise SourceSettlementConflict(
                "source settlement lacks settled admission authority"
            )
        settlement_data = self._require_support_document(
            transaction=transaction,
            user_ref=user_ref,
            collection_name="sourceSettlements",
            document_id=canonical_source_id,
        )
        _validate_source_settlement_document(
            settlement_data,
            canonical_source_id=canonical_source_id,
            identity_data=identity_data,
            classification_data=bundle[1][1],
            owner_data=bundle[2][1],
            ledger_data=bundle[3][1],
            current_aliases=_validated_identity_descriptors(
                identity_data,
                canonical_source_id=canonical_source_id,
            ),
        )

    def _audit_source_authority_support_document(
        self,
        *,
        transaction,
        user_ref,
        collection_name: str,
        document_id: str,
    ) -> None:
        if collection_name == "sourceAliases":
            self._audit_source_alias_support(
                transaction=transaction,
                user_ref=user_ref,
                alias_key=document_id,
            )
            return
        if collection_name == "sourceClassifications":
            self._audit_source_classification_support(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=document_id,
            )
            return
        if collection_name == "sourceTransitionOwners":
            self._audit_source_owner_support(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=document_id,
            )
            return
        if collection_name == "sourceWorkLedgers":
            self._audit_source_ledger_support(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=document_id,
            )
            return
        if collection_name == "sourceDeferredWork":
            self._audit_source_deferred_work_support(
                transaction=transaction,
                user_ref=user_ref,
                work_key=document_id,
            )
            return
        if collection_name == "blockedSources":
            self._audit_blocked_source_support(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=document_id,
            )
            return
        if collection_name == "sourceSettlements":
            self._audit_source_settlement_support(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=document_id,
            )
            return
        raise SourceCoordinatorConfigError(
            "source authority support collection is unsupported"
        )

    def _audit_source_authority_support_collection(
        self,
        *,
        user_ref,
        expected_revision: int | None,
        collection_name: str,
        page_size: int,
    ) -> None:
        query = user_ref.collection(collection_name).order_by("__name__")
        for page in self._source_authority_query_pages(
            user_ref=user_ref,
            expected_revision=expected_revision,
            query=query,
            page_size=page_size,
        ):
            for snapshot in page:
                self._run_source_authority_audit_read(
                    user_ref=user_ref,
                    expected_revision=expected_revision,
                    read=lambda transaction, document_id=snapshot.id: (
                        self._audit_source_authority_support_document(
                            transaction=transaction,
                            user_ref=user_ref,
                            collection_name=collection_name,
                            document_id=document_id,
                        )
                    ),
                )

    def _audit_thread_head(
        self,
        *,
        transaction,
        user_ref,
        thread_id: str,
    ) -> str | None:
        head_data = _snapshot_data(
            user_ref.collection("threadTransitionHeads")
            .document(thread_id)
            .get(transaction=transaction)
        )
        if head_data is None:
            raise SourceCoordinatorAmbiguous(
                "thread head audit returned an absent document"
            )
        _validate_thread_head_document(head_data, thread_id=thread_id)
        if head_data["activeState"] == "clear":
            return None
        canonical_source_id = head_data["activeCanonicalSourceId"]
        admission_data = _snapshot_data(
            user_ref.collection("inboundPendingAdmissions")
            .document(canonical_source_id)
            .get(transaction=transaction)
        )
        if admission_data is None:
            raise ThreadTransitionConflict(
                "actionable thread head lacks source admission"
            )
        _validate_pending_admission_document(
            admission_data,
            canonical_source_id=canonical_source_id,
            thread_id=thread_id,
        )
        bundle = self._read_source_authority_bundle(
            transaction=transaction,
            user_ref=user_ref,
            canonical_source_id=canonical_source_id,
            expected_thread_id=thread_id,
        )
        _validate_admission_authority_bindings(
            admission_data,
            identity_data=bundle[0][1],
            classification_data=bundle[1][1],
            owner_data=bundle[2][1],
            ledger_data=bundle[3][1],
        )
        owner_data = bundle[2][1]
        if (
            head_data["activeOwnerKind"] != owner_data["ownerKind"]
            or head_data["activeOwnerKey"] != owner_data["ownerKey"]
        ):
            raise ThreadTransitionConflict(
                "actionable thread head conflicts with source owner authority"
            )
        if head_data["activeState"] == "releasing":
            if admission_data["admissionState"] != "settled":
                raise WakeReleaseConflict(
                    "releasing thread head lacks settled source authority"
                )
        elif admission_data["admissionState"] not in {"processing", "settled"}:
            raise ThreadTransitionConflict(
                "active thread head lacks processing source authority"
            )
        return canonical_source_id

    def _source_authority_audit(
        self,
        *,
        user_id: str,
        max_records: int,
        page_size: int,
    ) -> tuple[int | None, tuple[str, ...]]:
        user_ref = self._firestore.collection("users").document(user_id)
        try:
            _sync_ref, sync_data, expected_revision = (
                self._read_source_authority_revision(
                    transaction=None,
                    user_ref=user_ref,
                    allow_missing=True,
                )
            )
        except SourceCoordinatorError:
            raise
        except Exception as revision_error:
            raise SourceCoordinatorRetryable(
                "source authority revision is unavailable"
            ) from revision_error
        del sync_data
        unsettled: set[str] = set()

        def add_unsettled(canonical_source_id: str) -> None:
            unsettled.add(canonical_source_id)
            if len(unsettled) > max_records:
                raise SourceCoordinatorConfigError(
                    "unsettled canonical source count exceeds its safe bound"
                )

        identity_query = user_ref.collection("sourceIdentities").order_by(
            "__name__"
        )
        for page in self._source_authority_query_pages(
            user_ref=user_ref,
            expected_revision=expected_revision,
            query=identity_query,
            page_size=page_size,
        ):
            for snapshot in page:
                canonical_source_id = snapshot.id
                is_unsettled = self._run_source_authority_audit_read(
                    user_ref=user_ref,
                    expected_revision=expected_revision,
                    read=lambda transaction, source_id=canonical_source_id: (
                        self._audit_source_identity(
                            transaction=transaction,
                            user_ref=user_ref,
                            canonical_source_id=source_id,
                        )
                    ),
                )
                if is_unsettled:
                    add_unsettled(canonical_source_id)

        admission_query = user_ref.collection(
            "inboundPendingAdmissions"
        ).order_by("__name__")
        for page in self._source_authority_query_pages(
            user_ref=user_ref,
            expected_revision=expected_revision,
            query=admission_query,
            page_size=page_size,
        ):
            for snapshot in page:
                canonical_source_id = snapshot.id
                self._run_source_authority_audit_read(
                    user_ref=user_ref,
                    expected_revision=expected_revision,
                    read=lambda transaction, source_id=canonical_source_id: (
                        self._audit_source_admission(
                            transaction=transaction,
                            user_ref=user_ref,
                            canonical_source_id=source_id,
                        )
                    ),
                )

        head_query = user_ref.collection("threadTransitionHeads").order_by(
            "__name__"
        )
        for page in self._source_authority_query_pages(
            user_ref=user_ref,
            expected_revision=expected_revision,
            query=head_query,
            page_size=page_size,
        ):
            for snapshot in page:
                actionable_source_id = self._run_source_authority_audit_read(
                    user_ref=user_ref,
                    expected_revision=expected_revision,
                    read=lambda transaction, head_id=snapshot.id: (
                        self._audit_thread_head(
                            transaction=transaction,
                            user_ref=user_ref,
                            thread_id=head_id,
                        )
                    ),
                )
                if actionable_source_id is not None:
                    add_unsettled(actionable_source_id)

        for collection_name in _SOURCE_AUTHORITY_SUPPORT_COLLECTIONS:
            self._audit_source_authority_support_collection(
                user_ref=user_ref,
                expected_revision=expected_revision,
                collection_name=collection_name,
                page_size=page_size,
            )

        self._run_source_authority_audit_read(
            user_ref=user_ref,
            expected_revision=expected_revision,
            read=lambda _transaction: None,
        )
        return expected_revision, tuple(sorted(unsettled))

    def unsettled_source_admission_ids(
        self,
        *,
        user_id: str,
        max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
        page_size: int = SOURCE_AUTHORITY_AUDIT_PAGE_SIZE,
    ) -> tuple[str, ...]:
        """Boundedly prove every retained canonical identity is settled."""
        _validate_user_id(user_id)
        max_records = _validate_positive_integer(
            max_records,
            field_name="unsettled admission audit limit",
        )
        page_size = _validate_positive_integer(
            page_size,
            field_name="source authority audit page size",
        )
        if page_size > SOURCE_AUTHORITY_AUDIT_PAGE_SIZE:
            raise SourceCoordinatorConfigError(
                "source authority audit page size exceeds its safe bound"
            )
        _revision, unsettled = self._source_authority_audit(
            user_id=user_id,
            max_records=max_records,
            page_size=page_size,
        )
        return unsettled

    def advance_scan_cursor_if_source_authority_clear(
        self,
        *,
        user_id: str,
        last_scan_iso: str,
        max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
        page_size: int = SOURCE_AUTHORITY_AUDIT_PAGE_SIZE,
    ) -> tuple[str, ...]:
        """Audit all B1 source authority before one fenced cursor write."""
        _validate_user_id(user_id)
        requested_cursor = _scan_cursor_datetime(
            last_scan_iso,
            field_name="last scan cursor",
        )
        max_records = _validate_positive_integer(
            max_records,
            field_name="unsettled admission audit limit",
        )
        page_size = _validate_positive_integer(
            page_size,
            field_name="source authority audit page size",
        )
        if page_size > SOURCE_AUTHORITY_AUDIT_PAGE_SIZE:
            raise SourceCoordinatorConfigError(
                "source authority audit page size exceeds its safe bound"
            )
        expected_revision, unsettled = self._source_authority_audit(
            user_id=user_id,
            max_records=max_records,
            page_size=page_size,
        )
        if unsettled:
            return unsettled

        user_ref = self._firestore.collection("users").document(user_id)
        expected_sync_data = None
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "source cursor transaction is unavailable"
            ) from transaction_error

        @transactional
        def advance_once(active_transaction):
            nonlocal expected_sync_data
            sync_ref, sync_before = self._require_source_authority_revision(
                transaction=active_transaction,
                user_ref=user_ref,
                expected_revision=expected_revision,
            )
            if expected_revision is None:
                self._require_empty_source_authority_inventory(
                    transaction=active_transaction,
                    user_ref=user_ref,
                )
            current_cursor_value = (
                sync_before.get("lastScanISO")
                if sync_before is not None
                else None
            )
            if current_cursor_value is not None:
                try:
                    current_cursor = _scan_cursor_datetime(
                        current_cursor_value,
                        field_name="stored last scan cursor",
                    )
                except SourceCoordinatorConfigError as cursor_error:
                    raise SourceCoordinatorAmbiguous(
                        "stored last scan cursor is malformed"
                    ) from cursor_error
                if current_cursor > requested_cursor:
                    return False
            expected_sync_data = (
                deepcopy(sync_before) if sync_before is not None else {}
            )
            expected_sync_data.update(
                {
                    "lastScanISO": last_scan_iso,
                    _SOURCE_AUTHORITY_AUDIT_REVISION_FIELD: (
                        0 if expected_revision is None else expected_revision
                    ),
                    "updatedAt": self._current_time(),
                }
            )
            active_transaction.set(sync_ref, expected_sync_data)
            return True

        try:
            advance_once(transaction)
            return ()
        except SourceCoordinatorError:
            raise
        except Exception as cursor_error:
            try:
                sync_data = _snapshot_data(self._source_sync_ref(user_ref).get())
                current_revision = _source_authority_revision_from_sync(
                    sync_data,
                    allow_missing=expected_revision is None,
                )
            except Exception as readback_error:
                raise SourceCoordinatorAmbiguous(
                    "source cursor commit outcome is unreadable"
                ) from readback_error
            audited_revision = 0 if expected_revision is None else expected_revision
            if (
                sync_data is not None
                and current_revision == expected_revision
                and sync_data.get("lastScanISO") == last_scan_iso
                and sync_data.get(_SOURCE_AUTHORITY_AUDIT_REVISION_FIELD)
                == audited_revision
            ):
                return ()
            if current_revision != expected_revision:
                raise SourceCoordinatorRetryable(
                    "source authority changed before cursor commit"
                ) from cursor_error
            if expected_sync_data is None or sync_data != expected_sync_data:
                raise SourceCoordinatorRetryable(
                    "source cursor commit was not applied"
                ) from cursor_error
            raise SourceCoordinatorAmbiguous(
                "source cursor commit outcome is ambiguous"
            ) from cursor_error

    def durable_source_resume_contexts(
        self,
        *,
        user_id: str,
        max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
    ) -> tuple[DurableSourceResumeContext, ...]:
        """Load claimed or claimable authority without provider mail state."""
        _validate_user_id(user_id)
        max_records = _validate_positive_integer(
            max_records,
            field_name="durable source resume limit",
        )
        user_ref = self._firestore.collection("users").document(user_id)
        admission_collection = user_ref.collection("inboundPendingAdmissions")
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "durable source resume transaction is unavailable"
            ) from transaction_error

        @transactional
        def load_once(active_transaction):
            snapshots_by_id = {}
            actionable_queries = (
                admission_collection.where(
                    "admissionState",
                    "==",
                    "pending",
                ).order_by("__name__"),
                admission_collection.where(
                    "admissionState",
                    "==",
                    "processing",
                ).order_by("__name__"),
                admission_collection.where(
                    "wakeState",
                    "==",
                    "eligible",
                ).order_by("__name__"),
            )
            for query in actionable_queries:
                for snapshot in active_transaction.get(
                    query.limit(max_records + 1)
                ):
                    snapshots_by_id.setdefault(snapshot.id, snapshot)
                    if len(snapshots_by_id) > max_records:
                        raise SourceCoordinatorConfigError(
                            "actionable durable source resume count exceeds its "
                            "safe bound"
                        )
            snapshots = [
                snapshots_by_id[canonical_source_id]
                for canonical_source_id in sorted(snapshots_by_id)
            ]
            contexts = []
            for snapshot in snapshots:
                canonical_source_id = snapshot.id
                admission_data = _snapshot_data(snapshot)
                if admission_data is None:
                    raise SourceCoordinatorAmbiguous(
                        "durable source resume returned an absent admission"
                    )
                thread_id = admission_data.get("threadId")
                _validate_document_id(thread_id, field_name="thread id")
                _validate_pending_admission_document(
                    admission_data,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                )
                is_eligible = (
                    admission_data["admissionState"] == "blocked"
                    and admission_data["blockedLifecycleState"] == "eligible"
                    and admission_data["wakeState"] == "eligible"
                )
                is_processing = admission_data["admissionState"] == "processing"
                is_pending = admission_data["admissionState"] == "pending"
                if not (is_eligible or is_processing or is_pending):
                    continue
                _validate_source_resume_bindings(
                    admission_data,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                )
                bundle = self._read_source_authority_bundle(
                    transaction=active_transaction,
                    user_ref=user_ref,
                    canonical_source_id=canonical_source_id,
                    expected_thread_id=thread_id,
                )
                identity_data = bundle[0][1]
                classification_data = bundle[1][1]
                owner_data = bundle[2][1]
                ledger_data = bundle[3][1]
                _validate_admission_authority_bindings(
                    admission_data,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                )
                if is_pending and owner_data["ownerKind"] != "none":
                    raise ThreadTransitionConflict(
                        "pending source resume requires an explicit none owner"
                    )
                if (is_eligible or is_processing) and (
                    owner_data["ownerKind"] == "none"
                ):
                    raise WakeClaimConflict(
                        "durable source resume cannot hold an explicit none owner"
                    )
                if is_processing:
                    head_data = _snapshot_data(
                        user_ref.collection("threadTransitionHeads")
                        .document(thread_id)
                        .get(transaction=active_transaction)
                    )
                    if head_data is None:
                        raise ThreadTransitionConflict(
                            "processing source resume lacks a thread head"
                        )
                    _validate_thread_head_document(
                        head_data,
                        thread_id=thread_id,
                    )
                    if (
                        head_data["activeState"] != "active"
                        or head_data["activeCanonicalSourceId"]
                        != canonical_source_id
                        or head_data["activeOwnerKind"]
                        != owner_data["ownerKind"]
                        or head_data["activeOwnerKey"] != owner_data["ownerKey"]
                    ):
                        raise ThreadTransitionConflict(
                            "processing source resume conflicts with its head"
                        )
                aliases = _validated_identity_descriptors(
                    identity_data,
                    canonical_source_id=canonical_source_id,
                )
                required_alias = next(
                    (
                        descriptor
                        for descriptor in aliases
                        if descriptor["aliasType"] == "graph"
                    ),
                    aliases[0],
                )
                history_binding = admission_data["savedHistoryBinding"]
                history_ref = (
                    user_ref.collection("threads")
                    .document(thread_id)
                    .collection("messages")
                    .document(history_binding["historyDocumentId"])
                )
                history_data = _snapshot_data(
                    history_ref.get(transaction=active_transaction)
                )
                if history_data is None:
                    raise SourceCoordinatorAmbiguous(
                        "durable source resume lacks retained history"
                    )
                _validate_retained_source_history_document(
                    history_data,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                    expected_history_hash=history_binding["historyHash"],
                )
                contexts.append(
                    DurableSourceResumeContext(
                        canonical_source_id=canonical_source_id,
                        thread_id=thread_id,
                        resume_state=(
                            "eligible"
                            if is_eligible
                            else "processing"
                            if is_processing
                            else "pending"
                        ),
                        received_at=admission_data["receivedAt"],
                        sent_at=admission_data["sentAt"],
                        saved_history_binding=_freeze_json(
                            deepcopy(admission_data["savedHistoryBinding"])
                        ),
                        index_binding=_freeze_json(
                            deepcopy(admission_data["indexBinding"])
                        ),
                        source_alias_keys=tuple(
                            descriptor["sourceAliasKey"]
                            for descriptor in aliases
                        ),
                        required_source_alias_key=required_alias[
                            "sourceAliasKey"
                        ],
                        snapshot=_classification_snapshot_from_data(
                            classification_data
                        ),
                        owner=_freeze_json(deepcopy(owner_data)),
                        ledger=_freeze_json(deepcopy(ledger_data)),
                        retained_history=_freeze_json(deepcopy(history_data)),
                    )
                )
            contexts.sort(
                key=lambda context: (
                    context.received_at,
                    context.sent_at,
                    context.thread_id,
                    context.canonical_source_id,
                )
            )
            return tuple(contexts)

        try:
            return load_once(transaction)
        except SourceCoordinatorError:
            raise
        except Exception as load_error:
            raise SourceCoordinatorRetryable(
                "durable source resume authority is unavailable"
            ) from load_error

    def settled_source_release_candidates(
        self,
        *,
        user_id: str,
        max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
    ) -> tuple[SettledSourceReleaseCandidate, ...]:
        """Load settled non-none owners whose thread release is recoverable."""
        _validate_user_id(user_id)
        max_records = _validate_positive_integer(
            max_records,
            field_name="settled source release limit",
        )
        user_ref = self._firestore.collection("users").document(user_id)
        admission_collection = user_ref.collection("inboundPendingAdmissions")
        settlement_collection = user_ref.collection("sourceSettlements")
        head_collection = user_ref.collection("threadTransitionHeads")
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "settled source release transaction is unavailable"
            ) from transaction_error

        @transactional
        def load_once(active_transaction):
            heads_by_thread_id = {}
            for active_state in ("active", "releasing"):
                query = (
                    head_collection.where(
                        "activeState",
                        "==",
                        active_state,
                    ).order_by("__name__")
                )
                for snapshot in active_transaction.get(
                    query.limit(max_records + 1)
                ):
                    heads_by_thread_id.setdefault(snapshot.id, snapshot)
                    if len(heads_by_thread_id) > max_records:
                        raise SourceCoordinatorConfigError(
                            "actionable settled source release count exceeds its "
                            "safe bound"
                        )
            candidates = []
            for thread_id in sorted(heads_by_thread_id):
                head_data = _snapshot_data(heads_by_thread_id[thread_id])
                if head_data is None:
                    raise SourceCoordinatorAmbiguous(
                        "settled source release returned an absent thread head"
                    )
                _validate_thread_head_document(
                    head_data,
                    thread_id=thread_id,
                )
                if head_data["activeState"] not in {"active", "releasing"}:
                    raise ThreadTransitionConflict(
                        "settled source release query returned an inactive head"
                    )
                canonical_source_id = head_data["activeCanonicalSourceId"]
                admission_data = _snapshot_data(
                    admission_collection.document(canonical_source_id).get(
                        transaction=active_transaction
                    )
                )
                if admission_data is None:
                    raise ThreadTransitionConflict(
                        "actionable thread head lacks source admission"
                    )
                _validate_pending_admission_document(
                    admission_data,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                )
                bundle = self._read_source_authority_bundle(
                    transaction=active_transaction,
                    user_ref=user_ref,
                    canonical_source_id=canonical_source_id,
                    expected_thread_id=thread_id,
                )
                identity_data = bundle[0][1]
                classification_data = bundle[1][1]
                owner_data = bundle[2][1]
                ledger_data = bundle[3][1]
                _validate_admission_authority_bindings(
                    admission_data,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                )
                if (
                    head_data["activeOwnerKind"] != owner_data["ownerKind"]
                    or head_data["activeOwnerKey"] != owner_data["ownerKey"]
                ):
                    raise ThreadTransitionConflict(
                        "actionable thread head conflicts with source owner authority"
                    )
                if admission_data["admissionState"] != "settled":
                    if head_data["activeState"] == "releasing":
                        raise WakeReleaseConflict(
                            "releasing thread head lacks settled source authority"
                        )
                    if admission_data["admissionState"] != "processing":
                        raise ThreadTransitionConflict(
                            "active thread head lacks processing source authority"
                        )
                    continue
                settlement_data = _snapshot_data(
                    settlement_collection.document(canonical_source_id).get(
                        transaction=active_transaction
                    )
                )
                if settlement_data is None:
                    raise SourceSettlementConflict(
                        "settled source release lacks settlement authority"
                    )
                _validate_source_settlement_document(
                    settlement_data,
                    canonical_source_id=canonical_source_id,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                    current_aliases=_validated_identity_descriptors(
                        identity_data,
                        canonical_source_id=canonical_source_id,
                    ),
                )
                candidates.append(
                    SettledSourceReleaseCandidate(
                        canonical_source_id=canonical_source_id,
                        thread_id=thread_id,
                    )
                )
            candidates.sort(
                key=lambda candidate: (
                    candidate.thread_id,
                    candidate.canonical_source_id,
                )
            )
            return tuple(candidates)

        try:
            return load_once(transaction)
        except SourceCoordinatorError:
            raise
        except Exception as load_error:
            raise SourceCoordinatorRetryable(
                "settled source release authority is unavailable"
            ) from load_error

    def consume_durable_source_resume_context(
        self,
        *,
        user_id: str,
        context: DurableSourceResumeContext,
        consumer=None,
    ) -> DurableSourceResumeResult:
        """Claim one durable wake and continue its frozen authority."""
        _validate_user_id(user_id)
        if type(context) is not DurableSourceResumeContext or (
            consumer is not None and not callable(consumer)
        ):
            raise SourceCoordinatorConfigError(
                "eligible source resume dependencies are invalid"
            )
        current = next(
            (
                candidate
                for candidate in self.durable_source_resume_contexts(
                    user_id=user_id
                )
                if candidate.canonical_source_id == context.canonical_source_id
            ),
            None,
        )
        if current is None or current != context:
            raise WakeClaimConflict(
                "eligible source resume context changed before claim"
            )
        if context.resume_state != "pending":
            transition = self.claim_or_resume_thread_transition(
                user_id=user_id,
                canonical_source_id=context.canonical_source_id,
                received_at=context.received_at,
                sent_at=context.sent_at,
                saved_history_binding=_thaw_json(context.saved_history_binding),
                index_binding=_thaw_json(context.index_binding),
            )
            if transition.disposition != "claimed":
                raise WakeClaimConflict(
                    "durable source resume did not claim its authority"
                )
        ledger = _thaw_json(context.ledger)
        work_result = self.consume_source_work_once(
            user_id=user_id,
            canonical_source_id=context.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
            consumer=(
                None
                if consumer is None
                else lambda execution_ledger: consumer(context, execution_ledger)
            ),
        )
        if work_result["state"] == "blocked":
            return DurableSourceResumeResult(
                canonical_source_id=context.canonical_source_id,
                thread_id=context.thread_id,
                state="blocked",
                settlement=None,
            )
        settlement = self.settle_source_markers_if_ready(
            user_id=user_id,
            canonical_source_id=context.canonical_source_id,
            ledger_hash=ledger["ledgerHash"],
            required_source_alias_key=context.required_source_alias_key,
        )
        if context.owner["ownerKind"] != "none":
            self.release_settled_generation_if_needed(
                user_id=user_id,
                thread_id=context.thread_id,
                canonical_source_id=context.canonical_source_id,
            )
        return DurableSourceResumeResult(
            canonical_source_id=context.canonical_source_id,
            thread_id=context.thread_id,
            state="settled",
            settlement=settlement,
        )

    def verify_settled_source_dispatch_binding(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        thread_id: str,
        source_alias_keys: Sequence[str],
        snapshot_hash: str,
        selection_hash: str,
        owner_kind: str,
        owner_key: str | None,
        ledger_hash: str,
        settlement_hash: str,
        settlement_revision: int,
        alias_projection_count: int,
    ) -> bool:
        """Prove a scanner result is bound to retained exact-source authority."""
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        _validate_document_id(thread_id, field_name="thread id")
        alias_keys = tuple(source_alias_keys)
        if (
            not alias_keys
            or len(alias_keys) > MAX_SOURCE_ALIASES
            or alias_keys != tuple(sorted(set(alias_keys)))
            or any(not _is_sha256(key) for key in alias_keys)
            or any(
                not _is_sha256(value)
                for value in (
                    snapshot_hash,
                    selection_hash,
                    ledger_hash,
                    settlement_hash,
                )
            )
            or owner_kind not in _TRANSITION_OWNER_KINDS
            or (owner_kind == "none") != (owner_key is None)
            or (owner_key is not None and not _is_sha256(owner_key))
            or type(settlement_revision) is not int
            or settlement_revision != 1
            or type(alias_projection_count) is not int
            or not 1 <= alias_projection_count <= MAX_SOURCE_ALIASES
        ):
            raise SourceCoordinatorConfigError(
                "settled dispatch binding is malformed"
            )
        user_ref = self._firestore.collection("users").document(user_id)
        admission_ref = user_ref.collection(
            "inboundPendingAdmissions"
        ).document(canonical_source_id)
        settlement_ref = user_ref.collection("sourceSettlements").document(
            canonical_source_id
        )
        source_alias_collection = user_ref.collection("sourceAliases")
        processed_collection = user_ref.collection("processedMessages")
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "settled dispatch verification transaction is unavailable"
            ) from transaction_error

        @transactional
        def verify_once(active_transaction):
            bundle = self._read_source_authority_bundle(
                transaction=active_transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
                expected_thread_id=thread_id,
            )
            identity_data = bundle[0][1]
            classification_data = bundle[1][1]
            owner_data = bundle[2][1]
            ledger_data = bundle[3][1]
            if (
                classification_data["snapshotImmutableHash"] != snapshot_hash
                or classification_data["selectionHash"] != selection_hash
                or owner_data["ownerKind"] != owner_kind
                or owner_data["ownerKey"] != owner_key
                or ledger_data["ledgerHash"] != ledger_hash
            ):
                raise SourceSettlementConflict(
                    "scanner authority conflicts with retained source authority"
                )
            aliases = _validated_identity_descriptors(
                identity_data,
                canonical_source_id=canonical_source_id,
            )
            descriptors_by_key = {
                descriptor["sourceAliasKey"]: descriptor
                for descriptor in aliases
            }
            if (
                alias_projection_count != len(aliases)
                or any(key not in descriptors_by_key for key in alias_keys)
            ):
                raise SourceSettlementConflict(
                    "scanner aliases conflict with retained source identity"
                )
            admission_data = _snapshot_data(
                admission_ref.get(transaction=active_transaction)
            )
            if admission_data is None:
                raise SourceSettlementConflict(
                    "scanner source lacks retained admission authority"
                )
            _validate_pending_admission_document(
                admission_data,
                canonical_source_id=canonical_source_id,
                thread_id=thread_id,
            )
            _validate_admission_authority_bindings(
                admission_data,
                identity_data=identity_data,
                classification_data=classification_data,
                owner_data=owner_data,
                ledger_data=ledger_data,
            )
            if admission_data["admissionState"] != "settled":
                raise SourceSettlementConflict(
                    "scanner source admission is not settled"
                )
            settlement_data = _snapshot_data(
                settlement_ref.get(transaction=active_transaction)
            )
            if settlement_data is None:
                raise SourceSettlementConflict(
                    "scanner source lacks retained settlement authority"
                )
            _validate_source_settlement_document(
                settlement_data,
                canonical_source_id=canonical_source_id,
                identity_data=identity_data,
                classification_data=classification_data,
                owner_data=owner_data,
                ledger_data=ledger_data,
                current_aliases=aliases,
            )
            if (
                settlement_data["settlementHash"] != settlement_hash
                or settlement_data["settlementRevision"]
                != settlement_revision
            ):
                raise SourceSettlementConflict(
                    "scanner settlement conflicts with retained settlement"
                )
            for alias_key in alias_keys:
                descriptor = descriptors_by_key[alias_key]
                alias_owner_data = _snapshot_data(
                    source_alias_collection.document(alias_key).get(
                        transaction=active_transaction
                    )
                )
                if alias_owner_data is None:
                    raise SourceSettlementConflict(
                        "scanner alias lacks retained owner authority"
                    )
                _validate_alias_projection(
                    alias_owner_data,
                    descriptor=descriptor,
                    canonical_source_id=canonical_source_id,
                )
                processed_data = _snapshot_data(
                    processed_collection.document(alias_key).get(
                        transaction=active_transaction
                    )
                )
                if processed_data is None:
                    raise SourceSettlementConflict(
                        "scanner alias lacks processed settlement projection"
                    )
                _validate_processed_alias_projection(
                    processed_data,
                    descriptor=descriptor,
                    canonical_source_id=canonical_source_id,
                    settlement_data=settlement_data,
                )
            return True

        try:
            return verify_once(transaction)
        except SourceCoordinatorError:
            raise
        except Exception as verification_error:
            raise SourceCoordinatorRetryable(
                "settled dispatch verification is unavailable"
            ) from verification_error

    def quarantine_retained_terminal_authority(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        thread_id: str,
        graph_message_id: str | None,
        internet_message_id: str | None,
    ) -> RetainedTerminalAuthorityDisposition:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        validated_thread_id = _validate_thread_id(thread_id)
        if validated_thread_id is None:
            raise SourceCoordinatorConfigError(
                "retained terminal quarantine requires an exact thread id"
            )
        supplied_aliases = []
        for alias_type, value in (
            ("graph", graph_message_id),
            ("internet_message_id", internet_message_id),
        ):
            if value is None:
                continue
            normalized = normalize_source_alias(alias_type, value)
            supplied_aliases.append(
                SourceAlias(
                    alias_type=normalized.alias_type,
                    value=normalized.value,
                    key=source_alias_key(user_id, normalized),
                )
            )
        if not supplied_aliases:
            raise SourceIdentityMissing(
                "retained terminal quarantine requires a typed source alias"
            )
        supplied_aliases = tuple(
            sorted(supplied_aliases, key=lambda alias: alias.key)
        )
        if self._retained_terminal_authority_loader is None:
            raise SourceCoordinatorConfigError(
                "retained terminal authority loader is unavailable"
            )

        user_ref = self._firestore.collection("users").document(user_id)
        thread_ref = user_ref.collection("threads").document(validated_thread_id)
        identity_ref = user_ref.collection("sourceIdentities").document(
            canonical_source_id
        )
        alias_collection = user_ref.collection("sourceAliases")
        classification_ref = user_ref.collection("sourceClassifications").document(
            canonical_source_id
        )

        def disposition(
            *,
            state: str,
            terminal_kind: str | None,
            evidence_hash: str | None,
            created: bool,
        ) -> RetainedTerminalAuthorityDisposition:
            return RetainedTerminalAuthorityDisposition(
                canonical_source_id=canonical_source_id,
                state=state,
                terminal_kind=terminal_kind,
                evidence_hash=evidence_hash,
                created=created,
            )

        def prepare(transaction):
            identity_data = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            identity_descriptors = _validated_identity_descriptors(
                identity_data,
                canonical_source_id=canonical_source_id,
            )
            if identity_data["threadId"] != validated_thread_id:
                raise SourceThreadConflict(
                    "retained terminal thread conflicts with source identity"
                )
            descriptors_by_key = {
                descriptor["sourceAliasKey"]: descriptor
                for descriptor in identity_descriptors
            }
            alias_prerequisites = {}
            for alias in supplied_aliases:
                descriptor = _alias_descriptor(alias)
                if descriptors_by_key.get(alias.key) != descriptor:
                    raise SourceAliasConflict(
                        "supplied source alias conflicts with canonical identity"
                    )
                alias_ref = alias_collection.document(alias.key)
                alias_data = _snapshot_data(
                    alias_ref.get(transaction=transaction)
                )
                if alias_data is None:
                    raise SourceAliasConflict(
                        "supplied source alias projection is missing"
                    )
                _validate_alias_projection(
                    alias_data,
                    descriptor=descriptor,
                    canonical_source_id=canonical_source_id,
                )
                alias_prerequisites[alias.key] = (alias_ref, alias_data)

            before = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if before is not None:
                _validate_classification_document(
                    before,
                    canonical_source_id=canonical_source_id,
                )
                state = before["classificationState"]
                if state == "snapshot_ready":
                    return _AuthorityCreatePlan(
                        result=disposition(
                            state="migrated_b1",
                            terminal_kind=None,
                            evidence_hash=None,
                            created=False,
                        ),
                        prerequisites=(
                            (identity_ref, deepcopy(identity_data)),
                            *(
                                (reference, deepcopy(data))
                                for reference, data in (
                                    alias_prerequisites[key]
                                    for key in sorted(alias_prerequisites)
                                )
                            ),
                        ),
                        target_ref=classification_ref,
                        before_data=before,
                        expected_data=before,
                        ambiguous_error_type=RetainedTerminalAuthorityConflict,
                    )
                if state not in {"claimed", "legacy_terminal_quarantined"}:
                    raise RetainedTerminalAuthorityConflict(
                        "classification authority blocks retained terminal quarantine"
                    )

            thread_data = _snapshot_data(
                thread_ref.get(transaction=transaction)
            )
            if thread_data is None:
                raise SourceCoordinatorRetryable(
                    "retained terminal authority thread is unavailable"
                )
            authority_prerequisites = (
                (identity_ref, deepcopy(identity_data)),
                (thread_ref, deepcopy(thread_data)),
                *(
                    (reference, deepcopy(data))
                    for reference, data in (
                        alias_prerequisites[key]
                        for key in sorted(alias_prerequisites)
                    )
                ),
            )
            try:
                loader_result = self._retained_terminal_authority_loader(
                    user_id,
                    validated_thread_id,
                    graph_message_id=graph_message_id,
                    internet_message_id=internet_message_id,
                    transaction=transaction,
                )
            except SourceCoordinatorError:
                raise
            except Exception as loader_error:
                raise SourceCoordinatorRetryable(
                    "retained terminal authority lookup is unavailable"
                ) from loader_error
            verified = _verified_retained_terminal_evidence_from_loader(
                loader_result,
                user_id=user_id,
                canonical_source_id=canonical_source_id,
                thread_id=validated_thread_id,
                thread_client_id=thread_data.get("clientId"),
                supplied_aliases=supplied_aliases,
                identity_descriptors=identity_descriptors,
            )
            if verified is None:
                if (
                    before is not None
                    and before["classificationState"]
                    == "legacy_terminal_quarantined"
                ):
                    raise RetainedTerminalAuthorityConflict(
                        "retained terminal evidence disappeared after quarantine"
                    )
                return _AuthorityCreatePlan(
                    result=disposition(
                        state="no_retained_terminal_authority",
                        terminal_kind=None,
                        evidence_hash=None,
                        created=False,
                    ),
                    prerequisites=authority_prerequisites,
                    target_ref=classification_ref,
                    before_data=before,
                    expected_data=before,
                    ambiguous_error_type=RetainedTerminalAuthorityConflict,
                )

            for alias in verified.aliases:
                if alias.key in alias_prerequisites:
                    continue
                alias_ref = alias_collection.document(alias.key)
                alias_data = _snapshot_data(
                    alias_ref.get(transaction=transaction)
                )
                if alias_data is None:
                    raise SourceAliasConflict(
                        "retained terminal alias projection is missing"
                    )
                _validate_alias_projection(
                    alias_data,
                    descriptor=_alias_descriptor(alias),
                    canonical_source_id=canonical_source_id,
                )
                alias_prerequisites[alias.key] = (alias_ref, alias_data)

            authority_prerequisites = (
                (identity_ref, deepcopy(identity_data)),
                (thread_ref, deepcopy(thread_data)),
                *(
                    (reference, deepcopy(data))
                    for reference, data in (
                        alias_prerequisites[key]
                        for key in sorted(alias_prerequisites)
                    )
                ),
            )

            result = disposition(
                state="legacy_terminal_authority_retained",
                terminal_kind=verified.terminal_kind,
                evidence_hash=verified.binding_hash,
                created=before is None,
            )
            retained_fields = {
                "retainedTerminalKind": verified.terminal_kind,
                "retainedTerminalImmutableHash": verified.immutable_hash,
                "retainedTerminalRecordHash": verified.record_hash,
                "retainedTerminalBindingHash": verified.binding_hash,
            }
            if (
                before is not None
                and before["classificationState"] != "legacy_terminal_quarantined"
            ):
                raise RetainedTerminalAuthorityConflict(
                    "classification claim conflicts with retained terminal authority"
                )
            if before is not None:
                if any(
                    before[field] != value
                    for field, value in retained_fields.items()
                ):
                    raise RetainedTerminalAuthorityConflict(
                        "retained terminal evidence conflicts with quarantine"
                    )
                expected = before
            else:
                now = self._current_time()
                expected = {
                    "schemaVersion": _CLASSIFICATION_SCHEMA_VERSION,
                    "canonicalSourceId": canonical_source_id,
                    "classificationState": "legacy_terminal_quarantined",
                    "classificationEpoch": 0,
                    "classificationClaimId": None,
                    "leaseExpiresAt": None,
                    "classificationInputSchemaVersion": _CLASSIFICATION_INPUT_SCHEMA_VERSION,
                    "classificationInputHash": None,
                    "modelRequestKey": None,
                    "modelRequestState": "not_applicable",
                    "requestStartFence": None,
                    **_empty_classification_snapshot_fields(),
                    **retained_fields,
                    "createdAt": now,
                    "updatedAt": now,
                }
                _validate_classification_document(
                    expected,
                    canonical_source_id=canonical_source_id,
                )
                transaction.create(classification_ref, expected)
            return _AuthorityCreatePlan(
                result=result,
                prerequisites=authority_prerequisites,
                target_ref=classification_ref,
                before_data=before,
                expected_data=expected,
                ambiguous_error_type=RetainedTerminalAuthorityConflict,
            )

        return self._run_authority_create_transaction(
            prepare,
            authority_name="retained terminal classification quarantine",
        )

    def claim_source_classification(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        lease_seconds: int,
    ) -> ClassificationClaim:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        lease_seconds = _validate_positive_integer(
            lease_seconds,
            field_name="classification lease seconds",
        )
        identity_ref, classification_ref = self._classification_refs(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
        )

        def prepare(transaction):
            identity_before = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            before = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            now = self._current_time()
            try:
                lease_expires_at = now + timedelta(seconds=lease_seconds)
            except OverflowError as exc:
                raise SourceCoordinatorConfigError(
                    "classification lease exceeds datetime range"
                ) from exc
            if before is None:
                epoch = 1
                created_at = now
            else:
                _validate_classification_document(
                    before,
                    canonical_source_id=canonical_source_id,
                )
                state = before["classificationState"]
                if state == "legacy_terminal_quarantined":
                    raise RetainedTerminalAuthorityConflict(
                        "retained terminal authority forbids fresh classification"
                    )
                if state == "snapshot_ready":
                    raise ClassificationSnapshotConflict(
                        "classification snapshot is already frozen"
                    )
                if state == "classification_request_ambiguous":
                    raise ClassificationRequestAmbiguous(
                        "classification request requires operator resolution"
                    )
                if state == "request_started":
                    if before["leaseExpiresAt"] <= now:
                        expected = deepcopy(before)
                        expected.update(
                            {
                                "classificationState": "classification_request_ambiguous",
                                "modelRequestState": "ambiguous",
                                "updatedAt": now,
                            }
                        )
                        transaction.update(classification_ref, expected)
                        return _ClassificationTransactionPlan(
                            result=_DeferredClassificationError(
                                ClassificationRequestAmbiguous(
                                    "expired started classification is ambiguous"
                                )
                            ),
                            identity_ref=identity_ref,
                            identity_data=identity_before,
                            classification_ref=classification_ref,
                            before_data=before,
                            expected_data=expected,
                            ambiguous_error_type=ClassificationRequestAmbiguous,
                        )
                    raise ClassificationRequestAmbiguous(
                        "classification request is already started"
                    )
                if before["leaseExpiresAt"] > now:
                    raise ClassificationClaimUnavailable(
                        "classification claim lease is still active"
                    )
                epoch = before["classificationEpoch"] + 1
                created_at = before["createdAt"]

            claim_id = self._allocate_document_token(
                field_name="classification claim id"
            )
            expected = {
                "schemaVersion": _CLASSIFICATION_SCHEMA_VERSION,
                "canonicalSourceId": canonical_source_id,
                "classificationState": "claimed",
                "classificationEpoch": epoch,
                "classificationClaimId": claim_id,
                "leaseExpiresAt": lease_expires_at,
                "classificationInputSchemaVersion": _CLASSIFICATION_INPUT_SCHEMA_VERSION,
                "classificationInputHash": None,
                "modelRequestKey": None,
                "modelRequestState": "not_started",
                "requestStartFence": None,
                **_empty_classification_snapshot_fields(),
                **_empty_retained_terminal_classification_fields(),
                "createdAt": created_at,
                "updatedAt": now,
            }
            result = _classification_claim_from_data(expected)
            if before is None:
                transaction.create(classification_ref, expected)
            else:
                transaction.update(classification_ref, expected)
            return _ClassificationTransactionPlan(
                result=result,
                identity_ref=identity_ref,
                identity_data=identity_before,
                classification_ref=classification_ref,
                before_data=before,
                expected_data=expected,
            )

        return self._run_classification_transaction(prepare)

    def record_classification_request_started(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        classification_epoch: int,
        classification_claim_id: str,
        model_request_key: str,
        classification_input: Mapping[str, Any],
    ) -> ClassificationRequestStart:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        classification_epoch, classification_claim_id = (
            _validate_classification_claim_coordinates(
                classification_epoch=classification_epoch,
                classification_claim_id=classification_claim_id,
            )
        )
        model_request_key = _validate_model_request_key(model_request_key)
        input_copy = _copy_exact_json_mapping(
            classification_input,
            field_name="classification input",
        )
        classification_input_hash = canonical_json_hash(input_copy)
        identity_ref, classification_ref = self._classification_refs(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
        )

        def result(*, newly_started):
            return ClassificationRequestStart(
                canonical_source_id=canonical_source_id,
                classification_epoch=classification_epoch,
                classification_claim_id=classification_claim_id,
                model_request_key=model_request_key,
                classification_input_hash=classification_input_hash,
                newly_started=newly_started,
            )

        def prepare(transaction):
            identity_before = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            before = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if before is None:
                raise ClassificationClaimConflict(
                    "classification claim does not exist"
                )
            _validate_classification_document(
                before,
                canonical_source_id=canonical_source_id,
            )
            if (
                before["classificationEpoch"] != classification_epoch
                or before["classificationClaimId"] != classification_claim_id
            ):
                raise ClassificationClaimConflict(
                    "classification claim coordinates do not match"
                )
            state = before["classificationState"]
            if state == "request_started":
                if before["classificationInputHash"] != classification_input_hash:
                    raise ClassificationInputConflict(
                        "classification input drifted after request start"
                    )
                if before["modelRequestKey"] != model_request_key:
                    raise ClassificationInputConflict(
                        "model request key conflicts with committed intent"
                    )
                return _ClassificationTransactionPlan(
                    result=result(newly_started=False),
                    identity_ref=identity_ref,
                    identity_data=identity_before,
                    classification_ref=classification_ref,
                    before_data=before,
                    expected_data=before,
                    ambiguous_error_type=ClassificationRequestAmbiguous,
                )
            if state == "classification_request_ambiguous":
                raise ClassificationRequestAmbiguous(
                    "classification request requires operator resolution"
                )
            if state == "snapshot_ready":
                if before["classificationInputHash"] != classification_input_hash:
                    raise ClassificationInputConflict(
                        "classification input conflicts with frozen snapshot"
                    )
                raise ClassificationSnapshotConflict(
                    "classification snapshot is already frozen"
                )
            if state == "legacy_terminal_quarantined":
                raise RetainedTerminalAuthorityConflict(
                    "retained terminal authority forbids request start"
                )
            if state != "claimed":
                raise ClassificationClaimConflict(
                    "classification claim is not startable"
                )
            now = self._current_time()
            if before["leaseExpiresAt"] <= now:
                raise ClassificationClaimExpired(
                    "classification claim expired before request start"
                )
            request_start_fence = self._allocate_document_token(
                field_name="request start fence"
            )
            expected = deepcopy(before)
            expected.update(
                {
                    "classificationState": "request_started",
                    "classificationInputHash": classification_input_hash,
                    "modelRequestKey": model_request_key,
                    "modelRequestState": "started",
                    "requestStartFence": request_start_fence,
                    "updatedAt": now,
                }
            )
            transaction.update(classification_ref, expected)
            return _ClassificationTransactionPlan(
                result=result(newly_started=True),
                identity_ref=identity_ref,
                identity_data=identity_before,
                classification_ref=classification_ref,
                before_data=before,
                expected_data=expected,
                ambiguous_error_type=ClassificationRequestAmbiguous,
            )

        return self._run_classification_transaction(prepare)

    def persist_complete_classification_snapshot(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        classification_epoch: int,
        classification_claim_id: str,
        complete_proposal: Mapping[str, Any],
        proposal_evidence: Mapping[str, Any],
    ) -> ClassificationSnapshot:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        classification_epoch, classification_claim_id = (
            _validate_classification_claim_coordinates(
                classification_epoch=classification_epoch,
                classification_claim_id=classification_claim_id,
            )
        )
        proposal_copy = _copy_exact_json_mapping(
            complete_proposal,
            field_name="complete proposal",
        )
        evidence_copy = _copy_exact_json_mapping(
            proposal_evidence,
            field_name="proposal evidence",
        )
        identity_ref, classification_ref = self._classification_refs(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
        )

        def prepare(transaction):
            identity_before = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            before = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if before is None:
                raise ClassificationClaimConflict(
                    "classification claim does not exist"
                )
            _validate_classification_document(
                before,
                canonical_source_id=canonical_source_id,
            )
            if (
                before["classificationEpoch"] != classification_epoch
                or before["classificationClaimId"] != classification_claim_id
            ):
                raise ClassificationClaimConflict(
                    "classification claim coordinates do not match"
                )
            if before["classificationState"] == "classification_request_ambiguous":
                raise ClassificationRequestAmbiguous(
                    "classification request requires operator resolution"
                )
            if before["classificationState"] not in {
                "request_started",
                "snapshot_ready",
            }:
                raise ClassificationClaimConflict(
                    "classification request has not started"
                )
            if before["modelRequestState"] not in {"started", "captured"}:
                raise ClassificationSnapshotConflict(
                    "model classification lane conflicts with authority"
                )
            material = _build_classification_snapshot_material(
                canonical_source_id=canonical_source_id,
                classification_input_hash=before["classificationInputHash"],
                model_request_key=before["modelRequestKey"],
                complete_proposal=proposal_copy,
                proposal_evidence=evidence_copy,
                deterministic_evidence=None,
            )
            if before["classificationState"] == "snapshot_ready":
                if any(before.get(field) != value for field, value in material.items()):
                    raise ClassificationSnapshotConflict(
                        "classification snapshot retry differs from authority"
                    )
                return _ClassificationTransactionPlan(
                    result=_classification_snapshot_from_data(before),
                    identity_ref=identity_ref,
                    identity_data=identity_before,
                    classification_ref=classification_ref,
                    before_data=before,
                    expected_data=before,
                )
            now = self._current_time()
            expected = deepcopy(before)
            expected.update(
                {
                    "classificationState": "snapshot_ready",
                    "modelRequestState": "captured",
                    **material,
                    "snapshotPersistedAt": now,
                    "updatedAt": now,
                }
            )
            transaction.update(classification_ref, expected)
            return _ClassificationTransactionPlan(
                result=_classification_snapshot_from_data(expected),
                identity_ref=identity_ref,
                identity_data=identity_before,
                classification_ref=classification_ref,
                before_data=before,
                expected_data=expected,
            )

        return self._run_classification_transaction(prepare)

    def _mark_classification_request_ambiguous(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        classification_epoch: int,
        classification_claim_id: str,
        model_request_key: str,
        classification_input_hash: str,
    ) -> None:
        identity_ref, classification_ref = self._classification_refs(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
        )

        def prepare(transaction):
            identity_before = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            before = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if before is None:
                raise ClassificationRequestAmbiguous(
                    "started classification authority is missing"
                )
            _validate_classification_document(
                before,
                canonical_source_id=canonical_source_id,
            )
            if (
                before["classificationEpoch"] != classification_epoch
                or before["classificationClaimId"] != classification_claim_id
                or before["classificationInputHash"] != classification_input_hash
                or before["modelRequestKey"] != model_request_key
            ):
                raise ClassificationRequestAmbiguous(
                    "started classification authority no longer matches request"
                )
            if before["classificationState"] == "classification_request_ambiguous":
                return _ClassificationTransactionPlan(
                    result=None,
                    identity_ref=identity_ref,
                    identity_data=identity_before,
                    classification_ref=classification_ref,
                    before_data=before,
                    expected_data=before,
                    ambiguous_error_type=ClassificationRequestAmbiguous,
                )
            if before["classificationState"] != "request_started":
                raise ClassificationRequestAmbiguous(
                    "classification request can no longer be marked ambiguous"
                )
            now = self._current_time()
            expected = deepcopy(before)
            expected.update(
                {
                    "classificationState": "classification_request_ambiguous",
                    "modelRequestState": "ambiguous",
                    "updatedAt": now,
                }
            )
            transaction.update(classification_ref, expected)
            return _ClassificationTransactionPlan(
                result=None,
                identity_ref=identity_ref,
                identity_data=identity_before,
                classification_ref=classification_ref,
                before_data=before,
                expected_data=expected,
                ambiguous_error_type=ClassificationRequestAmbiguous,
            )

        self._run_classification_transaction(prepare)

    def persist_deterministic_classification_snapshot(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        classification_epoch: int,
        classification_claim_id: str,
        classification_input: Mapping[str, Any],
    ) -> ClassificationSnapshot | None:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        classification_epoch, classification_claim_id = (
            _validate_classification_claim_coordinates(
                classification_epoch=classification_epoch,
                classification_claim_id=classification_claim_id,
            )
        )
        input_copy = _copy_exact_json_mapping(
            classification_input,
            field_name="classification input",
        )
        classification_input_hash = canonical_json_hash(input_copy)
        identity_ref, classification_ref = self._classification_refs(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
        )

        def validate_verifier_result(
            verified_result,
            *,
            verifier_name: str,
            hard_optout: bool,
        ):
            if verified_result is None:
                return None
            if (
                type(verified_result) is not dict
                or any(type(field) is not str for field in verified_result)
                or set(verified_result) != _HARD_OPTOUT_EVIDENCE_FIELDS
                or not _is_exact_schema_version(
                    verified_result.get("schemaVersion"),
                    _CLASSIFICATION_SNAPSHOT_SCHEMA_VERSION,
                )
                or type(verified_result.get("evidenceKind")) is not str
                or not verified_result.get("evidenceKind")
                or not _is_sha256(verified_result.get("evidenceHash"))
            ):
                raise SourceCoordinatorConfigError(
                    f"{verifier_name} returned an untrusted result"
                )
            if hard_optout:
                if (
                    verified_result["evidenceKind"]
                    in _LOCAL_SOURCE_POLICY_EVIDENCE_KINDS
                ):
                    raise SourceCoordinatorConfigError(
                        "hard opt-out verifier returned local-policy evidence"
                    )
            elif (
                verified_result["evidenceKind"]
                not in _LOCAL_SOURCE_POLICY_EVIDENCE_KINDS
            ):
                raise SourceCoordinatorConfigError(
                    "local source-policy verifier returned unsupported evidence"
                )
            return _VerifiedDeterministicEvidence(
                evidence=verified_result,
                hard_optout=hard_optout,
            )

        def invoke_verifier():
            if self._hard_optout_verifier is not None:
                try:
                    verified_result = self._hard_optout_verifier(
                        _freeze_json(deepcopy(input_copy))
                    )
                except SourceCoordinatorError:
                    raise
                except Exception as verifier_error:
                    raise SourceCoordinatorAmbiguous(
                        "hard opt-out verifier failed"
                    ) from verifier_error
                verified = validate_verifier_result(
                    verified_result,
                    verifier_name="hard opt-out verifier",
                    hard_optout=True,
                )
                if verified is not None:
                    return verified
            if self._local_source_policy_verifier is None:
                return None
            try:
                verified_result = self._local_source_policy_verifier(
                    _freeze_json(deepcopy(input_copy))
                )
            except SourceCoordinatorError:
                raise
            except Exception as verifier_error:
                raise SourceCoordinatorAmbiguous(
                    "local source-policy verifier failed"
                ) from verifier_error
            return validate_verifier_result(
                verified_result,
                verifier_name="local source-policy verifier",
                hard_optout=False,
            )

        def material_from_verified(verified_result):
            deterministic_evidence = _thaw_json(verified_result.evidence)
            if verified_result.hard_optout:
                complete_proposal = _deterministic_hard_optout_proposal(
                    deterministic_evidence
                )
            else:
                complete_proposal = {
                    "schemaVersion": _CLASSIFICATION_SNAPSHOT_SCHEMA_VERSION,
                    "transitionCandidates": [],
                    "ordinaryObligations": [],
                }
            return _build_classification_snapshot_material(
                canonical_source_id=canonical_source_id,
                classification_input_hash=classification_input_hash,
                model_request_key=None,
                complete_proposal=complete_proposal,
                proposal_evidence=None,
                deterministic_evidence=deterministic_evidence,
            )

        def prepare(transaction):
            identity_before = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            before = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if before is None:
                raise ClassificationClaimConflict(
                    "classification claim does not exist"
                )
            _validate_classification_document(
                before,
                canonical_source_id=canonical_source_id,
            )
            if (
                before["classificationEpoch"] != classification_epoch
                or before["classificationClaimId"] != classification_claim_id
            ):
                raise ClassificationClaimConflict(
                    "classification claim coordinates do not match"
                )
            now = self._current_time()
            if before["classificationState"] == "snapshot_ready":
                if (
                    before["classificationInputHash"] != classification_input_hash
                    or before["modelRequestState"] != "not_applicable"
                ):
                    raise ClassificationSnapshotConflict(
                        "deterministic snapshot retry differs from authority"
                    )
                if (
                    self._hard_optout_verifier is None
                    and self._local_source_policy_verifier is None
                ):
                    raise ClassificationSnapshotConflict(
                        "deterministic evidence verifier is unavailable on retry"
                    )
                verified = invoke_verifier()
                if verified is None:
                    raise ClassificationSnapshotConflict(
                        "deterministic evidence disappeared on retry"
                    )
                material = material_from_verified(verified)
                if any(
                    before.get(field) != value
                    for field, value in material.items()
                ):
                    raise ClassificationSnapshotConflict(
                        "deterministic snapshot retry differs from authority"
                    )
                return _ClassificationTransactionPlan(
                    result=_classification_snapshot_from_data(before),
                    identity_ref=identity_ref,
                    identity_data=identity_before,
                    classification_ref=classification_ref,
                    before_data=before,
                    expected_data=before,
                )
            if before["classificationState"] != "claimed":
                raise ClassificationRequestAmbiguous(
                    "model request state blocks deterministic classification"
                )
            if before["leaseExpiresAt"] <= now:
                raise ClassificationClaimExpired(
                    "classification claim expired before deterministic capture"
                )
            verified = invoke_verifier()
            if verified is None:
                return _ClassificationTransactionPlan(
                    result=None,
                    identity_ref=identity_ref,
                    identity_data=identity_before,
                    classification_ref=classification_ref,
                    before_data=before,
                    expected_data=before,
                )
            material = material_from_verified(verified)
            expected = deepcopy(before)
            expected.update(
                {
                    "classificationState": "snapshot_ready",
                    "classificationInputHash": classification_input_hash,
                    "modelRequestKey": None,
                    "modelRequestState": "not_applicable",
                    "requestStartFence": None,
                    **material,
                    "snapshotPersistedAt": now,
                    "updatedAt": now,
                }
            )
            transaction.update(classification_ref, expected)
            return _ClassificationTransactionPlan(
                result=_classification_snapshot_from_data(expected),
                identity_ref=identity_ref,
                identity_data=identity_before,
                classification_ref=classification_ref,
                before_data=before,
                expected_data=expected,
            )

        return self._run_classification_transaction(prepare)

    def _require_authoritative_classification_snapshot(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        expected_classification_input_hash: str | None,
    ) -> ClassificationSnapshot:
        identity_ref, classification_ref = self._classification_refs(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
        )
        self._require_source_identity_snapshot(
            identity_ref.get(),
            canonical_source_id=canonical_source_id,
        )
        data = _snapshot_data(classification_ref.get())
        if data is None:
            raise ClassificationSnapshotNotReady(
                "classification snapshot does not exist"
            )
        _validate_classification_document(
            data,
            canonical_source_id=canonical_source_id,
        )
        state = data["classificationState"]
        stored_input_hash = data.get("classificationInputHash")
        input_conflicts = (
            expected_classification_input_hash is not None
            and stored_input_hash is not None
            and stored_input_hash != expected_classification_input_hash
        )
        if state == "snapshot_ready":
            if input_conflicts:
                raise ClassificationInputConflict(
                    "classification input conflicts with retained authority"
                )
            return _classification_snapshot_from_data(data)
        if state == "legacy_terminal_quarantined":
            raise RetainedTerminalAuthorityConflict(
                "retained terminal authority is quarantined outside B1 classification"
            )
        if state in {
            "request_started",
            "classification_request_ambiguous",
        }:
            raise ClassificationRequestAmbiguous(
                "classification request is already started or ambiguous"
            )
        if input_conflicts:
            raise ClassificationInputConflict(
                "classification input conflicts with retained authority"
            )
        raise ClassificationSnapshotNotReady(
            "classification snapshot is not ready"
        )

    def _recover_expired_classification_request(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        classification_input_hash: str,
    ) -> ClassificationSnapshot:
        identity_ref, classification_ref = self._classification_refs(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
        )

        def prepare(transaction):
            identity_before = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            before = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if before is None:
                raise ClassificationSnapshotNotReady(
                    "classification authority disappeared during recovery"
                )
            _validate_classification_document(
                before,
                canonical_source_id=canonical_source_id,
            )
            state = before["classificationState"]
            input_conflicts = (
                before.get("classificationInputHash") is not None
                and before["classificationInputHash"] != classification_input_hash
            )
            if state == "snapshot_ready":
                if input_conflicts:
                    raise ClassificationInputConflict(
                        "classification input conflicts with retained authority"
                    )
                return _ClassificationTransactionPlan(
                    result=_classification_snapshot_from_data(before),
                    identity_ref=identity_ref,
                    identity_data=identity_before,
                    classification_ref=classification_ref,
                    before_data=before,
                    expected_data=before,
                    ambiguous_error_type=ClassificationRequestAmbiguous,
                )
            if state == "classification_request_ambiguous":
                raise ClassificationRequestAmbiguous(
                    "classification request requires operator resolution"
                )
            if state != "request_started":
                raise ClassificationSnapshotNotReady(
                    "classification request is no longer started"
                )
            now = self._current_time()
            if before["leaseExpiresAt"] > now:
                if input_conflicts:
                    raise ClassificationInputConflict(
                        "classification input conflicts with request authority"
                    )
                raise ClassificationRequestAmbiguous(
                    "classification request is still active"
                )
            expected = deepcopy(before)
            expected.update(
                {
                    "classificationState": "classification_request_ambiguous",
                    "modelRequestState": "ambiguous",
                    "updatedAt": now,
                }
            )
            transaction.update(classification_ref, expected)
            return _ClassificationTransactionPlan(
                result=_DeferredClassificationError(
                    ClassificationRequestAmbiguous(
                        "expired classification request requires operator resolution"
                    )
                ),
                identity_ref=identity_ref,
                identity_data=identity_before,
                classification_ref=classification_ref,
                before_data=before,
                expected_data=expected,
                ambiguous_error_type=ClassificationRequestAmbiguous,
            )

        return self._run_classification_transaction(prepare)

    def require_authoritative_classification_snapshot(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
    ) -> ClassificationSnapshot:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        return self._require_authoritative_classification_snapshot(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            expected_classification_input_hash=None,
        )

    def elect_transition_owner_from_snapshot(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        expected_owner_kind: str | None = None,
    ) -> Mapping[str, Any]:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        if (
            expected_owner_kind is not None
            and (
                type(expected_owner_kind) is not str
                or expected_owner_kind not in _TRANSITION_OWNER_KINDS
            )
        ):
            raise SourceCoordinatorConfigError(
                "expected owner kind is unsupported"
            )
        user_ref = self._firestore.collection("users").document(user_id)
        identity_ref = user_ref.collection("sourceIdentities").document(
            canonical_source_id
        )
        classification_ref = user_ref.collection("sourceClassifications").document(
            canonical_source_id
        )
        owner_ref = user_ref.collection("sourceTransitionOwners").document(
            canonical_source_id
        )

        def prepare(transaction):
            identity_data = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            classification_data = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if classification_data is None:
                raise ClassificationSnapshotNotReady(
                    "transition owner requires a classification snapshot"
                )
            _validate_classification_document(
                classification_data,
                canonical_source_id=canonical_source_id,
            )
            if classification_data["classificationState"] != "snapshot_ready":
                raise ClassificationSnapshotNotReady(
                    "transition owner requires snapshot_ready authority"
                )
            immutable = _transition_owner_immutable_material(classification_data)
            if (
                expected_owner_kind is not None
                and immutable["ownerKind"] != expected_owner_kind
            ):
                raise TransitionOwnerConflict(
                    "expected owner kind does not match stored selection"
                )
            before = _snapshot_data(owner_ref.get(transaction=transaction))
            if before is not None:
                _validate_transition_owner_document(
                    before,
                    canonical_source_id=canonical_source_id,
                    classification_data=classification_data,
                )
                return _AuthorityCreatePlan(
                    result=_freeze_json(deepcopy(before)),
                    prerequisites=(
                        (identity_ref, deepcopy(identity_data)),
                        (classification_ref, deepcopy(classification_data)),
                    ),
                    target_ref=owner_ref,
                    before_data=before,
                    expected_data=before,
                    ambiguous_error_type=TransitionOwnerConflict,
                )
            now = self._current_time()
            expected = {
                **immutable,
                "revision": 1,
                "createdAt": now,
                "updatedAt": now,
            }
            transaction.create(owner_ref, expected)
            return _AuthorityCreatePlan(
                result=_freeze_json(deepcopy(expected)),
                prerequisites=(
                    (identity_ref, deepcopy(identity_data)),
                    (classification_ref, deepcopy(classification_data)),
                ),
                target_ref=owner_ref,
                before_data=None,
                expected_data=expected,
                ambiguous_error_type=TransitionOwnerConflict,
            )

        return self._run_authority_create_transaction(
            prepare,
            authority_name="transition owner",
        )

    def create_or_verify_source_work_ledger(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
    ) -> Mapping[str, Any]:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        user_ref = self._firestore.collection("users").document(user_id)
        identity_ref = user_ref.collection("sourceIdentities").document(
            canonical_source_id
        )
        classification_ref = user_ref.collection("sourceClassifications").document(
            canonical_source_id
        )
        owner_ref = user_ref.collection("sourceTransitionOwners").document(
            canonical_source_id
        )
        ledger_ref = user_ref.collection("sourceWorkLedgers").document(
            canonical_source_id
        )

        def prepare(transaction):
            identity_data = self._require_source_identity_snapshot(
                identity_ref.get(transaction=transaction),
                canonical_source_id=canonical_source_id,
            )
            classification_data = _snapshot_data(
                classification_ref.get(transaction=transaction)
            )
            if classification_data is None:
                raise ClassificationSnapshotNotReady(
                    "source work ledger requires a classification snapshot"
                )
            _validate_classification_document(
                classification_data,
                canonical_source_id=canonical_source_id,
            )
            if classification_data["classificationState"] != "snapshot_ready":
                raise ClassificationSnapshotNotReady(
                    "source work ledger requires snapshot_ready authority"
                )
            owner_data = _snapshot_data(owner_ref.get(transaction=transaction))
            if owner_data is None:
                raise TransitionOwnerConflict(
                    "source work ledger requires an explicit owner decision"
                )
            _validate_transition_owner_document(
                owner_data,
                canonical_source_id=canonical_source_id,
                classification_data=classification_data,
            )
            immutable = _source_work_ledger_immutable_material(
                canonical_source_id=canonical_source_id,
                classification_data=classification_data,
                owner_data=owner_data,
            )
            before = _snapshot_data(ledger_ref.get(transaction=transaction))
            if before is not None:
                _validate_source_work_ledger_document(
                    before,
                    canonical_source_id=canonical_source_id,
                    classification_data=classification_data,
                    owner_data=owner_data,
                )
                return _AuthorityCreatePlan(
                    result=_freeze_json(deepcopy(before)),
                    prerequisites=(
                        (identity_ref, deepcopy(identity_data)),
                        (classification_ref, deepcopy(classification_data)),
                        (owner_ref, deepcopy(owner_data)),
                    ),
                    target_ref=ledger_ref,
                    before_data=before,
                    expected_data=before,
                    ambiguous_error_type=SourceWorkLedgerConflict,
                )
            now = self._current_time()
            expected = {
                **immutable,
                "createdAt": now,
                "updatedAt": now,
            }
            transaction.create(ledger_ref, expected)
            return _AuthorityCreatePlan(
                result=_freeze_json(deepcopy(expected)),
                prerequisites=(
                    (identity_ref, deepcopy(identity_data)),
                    (classification_ref, deepcopy(classification_data)),
                    (owner_ref, deepcopy(owner_data)),
                ),
                target_ref=ledger_ref,
                before_data=None,
                expected_data=expected,
                ambiguous_error_type=SourceWorkLedgerConflict,
            )

        return self._run_authority_create_transaction(
            prepare,
            authority_name="source work ledger",
        )

    def admit_pending_inbound(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        received_at: datetime,
        sent_at: datetime,
        saved_history_binding: Mapping[str, Any],
        index_binding: Mapping[str, Any],
    ) -> PendingAdmissionResult:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        _canonical_datetime_token(received_at, field_name="received at")
        _canonical_datetime_token(sent_at, field_name="sent at")
        user_ref = self._firestore.collection("users").document(user_id)
        admission_collection = user_ref.collection("inboundPendingAdmissions")
        admission_ref = admission_collection.document(canonical_source_id)

        def prepare(transaction):
            bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            (identity_ref, identity_data), (
                classification_ref,
                classification_data,
            ), (owner_ref, owner_data), (ledger_ref, ledger_data) = bundle
            thread_id = identity_data["threadId"]
            head_ref = user_ref.collection("threadTransitionHeads").document(
                thread_id
            )
            head_before = _snapshot_data(head_ref.get(transaction=transaction))
            if head_before is not None:
                _validate_thread_head_document(head_before, thread_id=thread_id)
            admissions = self._query_pending_admissions(
                transaction=transaction,
                collection_ref=admission_collection,
                thread_id=thread_id,
            )
            immutable = _pending_admission_immutable_material(
                canonical_source_id=canonical_source_id,
                thread_id=thread_id,
                identity_data=identity_data,
                classification_data=classification_data,
                owner_data=owner_data,
                ledger_data=ledger_data,
                received_at=received_at,
                sent_at=sent_at,
                saved_history_binding=saved_history_binding,
                index_binding=index_binding,
            )
            existing = next(
                (
                    data
                    for reference, data in admissions
                    if reference.id == canonical_source_id
                ),
                None,
            )
            authority_prerequisites = (
                (identity_ref, identity_data),
                (classification_ref, classification_data),
                (owner_ref, owner_data),
                (ledger_ref, ledger_data),
                (head_ref, head_before),
            )
            if existing is not None:
                _validate_pending_admission_document(
                    existing,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                    expected_immutable=immutable,
                )
                return _MultiDocumentTransactionPlan(
                    result=PendingAdmissionResult(
                        canonical_source_id=canonical_source_id,
                        thread_id=thread_id,
                        admission_hash=existing["admissionHash"],
                        state=existing["admissionState"],
                        created=False,
                    ),
                    prerequisites=(
                        *authority_prerequisites,
                        *((reference, data) for reference, data in admissions),
                    ),
                    mutations=(),
                    ambiguous_error_type=PendingAdmissionConflict,
                )
            if (
                owner_data["ownerKind"] != "none"
                and head_before is not None
                and head_before["activeState"] != "clear"
            ):
                raise ThreadTransitionConflict(
                    "occupied thread requires atomic blocked-source enqueue"
                )
            now = self._current_time()
            expected = _initial_pending_admission_document(immutable, now=now)
            mutations = ((admission_ref, None, expected),)
            mutations = self._stage_source_authority_mutations(
                transaction,
                user_ref=user_ref,
                mutations=mutations,
            )
            return _MultiDocumentTransactionPlan(
                result=PendingAdmissionResult(
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                    admission_hash=expected["admissionHash"],
                    state="pending",
                    created=True,
                ),
                prerequisites=(
                    *authority_prerequisites,
                    *((reference, data) for reference, data in admissions),
                ),
                mutations=mutations,
                ambiguous_error_type=PendingAdmissionConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="pending inbound admission",
        )

    def enqueue_blocked_source(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        received_at: datetime,
        sent_at: datetime,
        saved_history_binding: Mapping[str, Any],
        index_binding: Mapping[str, Any],
    ) -> ThreadTransitionResult:
        return self._claim_or_block_thread_transition(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            received_at=received_at,
            sent_at=sent_at,
            saved_history_binding=saved_history_binding,
            index_binding=index_binding,
            required_disposition="blocked",
        )

    def claim_or_block_thread_transition(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        received_at: datetime | None = None,
        sent_at: datetime | None = None,
        saved_history_binding: Mapping[str, Any] | None = None,
        index_binding: Mapping[str, Any] | None = None,
    ) -> ThreadTransitionResult:
        return self._claim_or_block_thread_transition(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            received_at=received_at,
            sent_at=sent_at,
            saved_history_binding=saved_history_binding,
            index_binding=index_binding,
            required_disposition=None,
        )

    def _claim_or_block_thread_transition(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        received_at: datetime | None,
        sent_at: datetime | None,
        saved_history_binding: Mapping[str, Any] | None,
        index_binding: Mapping[str, Any] | None,
        required_disposition: str | None,
    ) -> ThreadTransitionResult:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        supplied_admission_values = (
            received_at,
            sent_at,
            saved_history_binding,
            index_binding,
        )
        has_all_admission_values = all(
            value is not None for value in supplied_admission_values
        )
        if any(value is not None for value in supplied_admission_values) and not (
            has_all_admission_values
        ):
            raise SourceCoordinatorConfigError(
                "inline pending admission requires all binding fields"
            )
        if has_all_admission_values:
            _canonical_datetime_token(received_at, field_name="received at")
            _canonical_datetime_token(sent_at, field_name="sent at")
        user_ref = self._firestore.collection("users").document(user_id)
        admission_collection = user_ref.collection("inboundPendingAdmissions")
        admission_ref = admission_collection.document(canonical_source_id)
        projection_ref = user_ref.collection("blockedSources").document(
            canonical_source_id
        )

        def prepare(transaction):
            bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            (identity_ref, identity_data), (
                classification_ref,
                classification_data,
            ), (owner_ref, owner_data), (ledger_ref, ledger_data) = bundle
            if owner_data["ownerKind"] == "none":
                raise ThreadTransitionConflict(
                    "explicit none owner does not require a transition head"
                )
            thread_id = identity_data["threadId"]
            head_ref = user_ref.collection("threadTransitionHeads").document(
                thread_id
            )
            head_before = _snapshot_data(head_ref.get(transaction=transaction))
            if head_before is not None:
                _validate_thread_head_document(head_before, thread_id=thread_id)
            admissions = self._query_pending_admissions(
                transaction=transaction,
                collection_ref=admission_collection,
                thread_id=thread_id,
            )
            admissions_by_id = {
                data["canonicalSourceId"]: (reference, data)
                for reference, data in admissions
            }
            admission_before = admissions_by_id.get(
                canonical_source_id,
                (admission_ref, None),
            )[1]
            immutable = None
            if has_all_admission_values:
                immutable = _pending_admission_immutable_material(
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                    received_at=received_at,
                    sent_at=sent_at,
                    saved_history_binding=saved_history_binding,
                    index_binding=index_binding,
                )
            if admission_before is None:
                if immutable is None:
                    raise PendingAdmissionConflict(
                        "thread transition requires a pending admission"
                    )
            else:
                _validate_pending_admission_document(
                    admission_before,
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                    expected_immutable=immutable,
                )
                _validate_admission_authority_bindings(
                    admission_before,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                )
            authority_prerequisites = [
                (identity_ref, identity_data),
                (classification_ref, classification_data),
                (owner_ref, owner_data),
                (ledger_ref, ledger_data),
            ]
            if head_before is not None and head_before["activeState"] != "clear":
                winner_source_id = head_before["activeCanonicalSourceId"]
                winner_bundle = self._read_source_authority_bundle(
                    transaction=transaction,
                    user_ref=user_ref,
                    canonical_source_id=winner_source_id,
                    expected_thread_id=thread_id,
                )
                winner_owner = winner_bundle[2][1]
                if (
                    head_before["activeOwnerKind"] != winner_owner["ownerKind"]
                    or head_before["activeOwnerKey"] != winner_owner["ownerKey"]
                ):
                    raise ThreadTransitionConflict(
                        "thread head conflicts with retained winner authority"
                    )
                winner_admission = admissions_by_id.get(winner_source_id)
                if winner_admission is None:
                    raise ThreadTransitionConflict(
                        "thread head has no authoritative pending admission"
                    )
                _validate_admission_authority_bindings(
                    winner_admission[1],
                    identity_data=winner_bundle[0][1],
                    classification_data=winner_bundle[1][1],
                    owner_data=winner_owner,
                    ledger_data=winner_bundle[3][1],
                )
                authority_prerequisites.extend(winner_bundle)
            now = self._current_time()
            if head_before is None or head_before["activeState"] == "clear":
                if required_disposition == "blocked":
                    raise ThreadTransitionConflict(
                        "blocked enqueue requires an occupied thread head"
                    )
                if admission_before is not None and admission_before[
                    "admissionState"
                ] != "pending":
                    raise ThreadTransitionConflict(
                        "thread claim conflicts with admission state"
                    )
                generation = (
                    1
                    if head_before is None
                    else head_before["activeGeneration"] + 1
                )
                head_expected = _build_thread_head_document(
                    thread_id=thread_id,
                    canonical_source_id=canonical_source_id,
                    owner_data=owner_data,
                    generation=generation,
                    state="active",
                    revision=(
                        1
                        if head_before is None
                        else head_before["threadHeadRevision"] + 1
                    ),
                    now=now,
                    created_at=(
                        None if head_before is None else head_before["createdAt"]
                    ),
                )
                if admission_before is None:
                    admission_expected = _initial_pending_admission_document(
                        immutable,
                        now=now,
                    )
                else:
                    admission_expected = deepcopy(admission_before)
                    admission_expected["revision"] += 1
                admission_expected.update(
                    {
                        "admissionState": "processing",
                        "updatedAt": now,
                    }
                )
                mutations = (
                    (head_ref, head_before, head_expected),
                    (admission_ref, admission_before, admission_expected),
                )
                mutation_refs = {head_ref, admission_ref}
                prerequisites = [
                    *authority_prerequisites,
                    *(
                        (reference, data)
                        for reference, data in admissions
                        if reference not in mutation_refs
                    ),
                ]
                mutations = self._stage_source_authority_mutations(
                    transaction,
                    user_ref=user_ref,
                    mutations=mutations,
                )
                return _MultiDocumentTransactionPlan(
                    result=ThreadTransitionResult(
                        canonical_source_id=canonical_source_id,
                        thread_id=thread_id,
                        disposition="claimed",
                        generation=generation,
                        head_revision=head_expected["threadHeadRevision"],
                        blocker_canonical_source_id=None,
                    ),
                    prerequisites=prerequisites,
                    mutations=mutations,
                    ambiguous_error_type=ThreadTransitionConflict,
                )
            if head_before["activeCanonicalSourceId"] == canonical_source_id:
                if required_disposition == "blocked":
                    raise ThreadTransitionConflict(
                        "source already owns the occupied thread head"
                    )
                if (
                    head_before["activeState"] != "active"
                    or head_before["activeOwnerKind"] != owner_data["ownerKind"]
                    or head_before["activeOwnerKey"] != owner_data["ownerKey"]
                    or admission_before is None
                    or admission_before["admissionState"] != "processing"
                ):
                    raise ThreadTransitionConflict(
                        "same-source thread-head retry conflicts with authority"
                    )
                return _MultiDocumentTransactionPlan(
                    result=ThreadTransitionResult(
                        canonical_source_id=canonical_source_id,
                        thread_id=thread_id,
                        disposition="claimed",
                        generation=head_before["activeGeneration"],
                        head_revision=head_before["threadHeadRevision"],
                        blocker_canonical_source_id=None,
                    ),
                    prerequisites=(
                        *authority_prerequisites,
                        (head_ref, head_before),
                        *((reference, data) for reference, data in admissions),
                    ),
                    mutations=(),
                    ambiguous_error_type=ThreadTransitionConflict,
                )
            blocker = _blocker_from_head(head_before)
            blocked_count = sum(
                data["admissionState"] == "blocked"
                and data["canonicalSourceId"] != canonical_source_id
                for _reference, data in admissions
            )
            if (
                admission_before is None
                or admission_before["admissionState"] == "pending"
            ) and blocked_count >= MAX_BLOCKED_SOURCES_PER_THREAD:
                raise ThreadQueueLimitExceeded(
                    "thread blocked-source capacity is exhausted"
                )
            projection_before = _snapshot_data(
                projection_ref.get(transaction=transaction)
            )
            if admission_before is not None and admission_before[
                "admissionState"
            ] == "blocked":
                if (
                    admission_before["blockedLifecycleState"] != "blocked"
                    or admission_before["wakeState"] != "none"
                    or admission_before["currentBlocker"] != blocker
                    or projection_before is None
                ):
                    raise ThreadTransitionConflict(
                        "blocked admission retry conflicts with current head"
                    )
                _validate_blocked_projection_document(
                    projection_before,
                    admission=admission_before,
                )
                return _MultiDocumentTransactionPlan(
                    result=ThreadTransitionResult(
                        canonical_source_id=canonical_source_id,
                        thread_id=thread_id,
                        disposition="blocked",
                        generation=head_before["activeGeneration"],
                        head_revision=head_before["threadHeadRevision"],
                        blocker_canonical_source_id=head_before[
                            "activeCanonicalSourceId"
                        ],
                    ),
                    prerequisites=(
                        *authority_prerequisites,
                        (head_ref, head_before),
                        *((reference, data) for reference, data in admissions),
                        (projection_ref, projection_before),
                    ),
                    mutations=(),
                    ambiguous_error_type=ThreadTransitionConflict,
                )
            if admission_before is not None and admission_before[
                "admissionState"
            ] != "pending":
                raise ThreadTransitionConflict(
                    "source cannot enter the blocked lifecycle from its state"
                )
            if projection_before is not None:
                raise ThreadTransitionConflict(
                    "blocked projection exists without blocked admission authority"
                )
            if admission_before is None:
                admission_expected = _initial_pending_admission_document(
                    immutable,
                    now=now,
                )
            else:
                admission_expected = deepcopy(admission_before)
                admission_expected["revision"] += 1
            admission_expected.update(
                {
                    "admissionState": "blocked",
                    "blockedLifecycleState": "blocked",
                    "initialBlocker": deepcopy(blocker),
                    "currentBlocker": deepcopy(blocker),
                    "updatedAt": now,
                }
            )
            projection_expected = _blocked_projection_from_admission(
                admission_expected,
                now=now,
            )
            mutations = (
                (admission_ref, admission_before, admission_expected),
                (projection_ref, None, projection_expected),
            )
            mutation_refs = {admission_ref, projection_ref}
            prerequisites = [
                *authority_prerequisites,
                (head_ref, head_before),
                *(
                    (reference, data)
                    for reference, data in admissions
                    if reference not in mutation_refs
                ),
            ]
            mutations = self._stage_source_authority_mutations(
                transaction,
                user_ref=user_ref,
                mutations=mutations,
            )
            return _MultiDocumentTransactionPlan(
                result=ThreadTransitionResult(
                    canonical_source_id=canonical_source_id,
                    thread_id=thread_id,
                    disposition="blocked",
                    generation=head_before["activeGeneration"],
                    head_revision=head_before["threadHeadRevision"],
                    blocker_canonical_source_id=head_before[
                        "activeCanonicalSourceId"
                    ],
                ),
                prerequisites=prerequisites,
                mutations=mutations,
                ambiguous_error_type=ThreadTransitionConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="thread transition claim",
        )

    def claim_or_resume_thread_transition(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        received_at: datetime,
        sent_at: datetime,
        saved_history_binding: Mapping[str, Any],
        index_binding: Mapping[str, Any],
    ) -> ThreadTransitionResult:
        """Atomically admit/queue a source, or consume its eligible wake token."""
        try:
            return self.claim_or_block_thread_transition(
                user_id=user_id,
                canonical_source_id=canonical_source_id,
                received_at=received_at,
                sent_at=sent_at,
                saved_history_binding=saved_history_binding,
                index_binding=index_binding,
            )
        except ThreadTransitionConflict as claim_error:
            _validate_user_id(user_id)
            _validate_document_id(
                canonical_source_id,
                field_name="canonical source id",
            )
            user_ref = self._firestore.collection("users").document(user_id)
            admission_ref = user_ref.collection(
                "inboundPendingAdmissions"
            ).document(canonical_source_id)
            try:
                admission_data = _snapshot_data(admission_ref.get())
                bundle = self._read_source_authority_bundle(
                    transaction=None,
                    user_ref=user_ref,
                    canonical_source_id=canonical_source_id,
                )
            except SourceCoordinatorError:
                raise
            except Exception as read_error:
                raise ThreadTransitionConflict(
                    "eligible wake authority is unreadable"
                ) from read_error
            if admission_data is None:
                raise claim_error
            thread_id = bundle[0][1]["threadId"]
            expected_immutable = _pending_admission_immutable_material(
                canonical_source_id=canonical_source_id,
                thread_id=thread_id,
                identity_data=bundle[0][1],
                classification_data=bundle[1][1],
                owner_data=bundle[2][1],
                ledger_data=bundle[3][1],
                received_at=received_at,
                sent_at=sent_at,
                saved_history_binding=saved_history_binding,
                index_binding=index_binding,
            )
            _validate_pending_admission_document(
                admission_data,
                canonical_source_id=canonical_source_id,
                thread_id=thread_id,
                expected_immutable=expected_immutable,
            )
            _validate_admission_authority_bindings(
                admission_data,
                identity_data=bundle[0][1],
                classification_data=bundle[1][1],
                owner_data=bundle[2][1],
                ledger_data=bundle[3][1],
            )
            if (
                admission_data["admissionState"] != "blocked"
                or admission_data["blockedLifecycleState"] != "eligible"
                or admission_data["wakeState"] != "eligible"
                or not _is_sha256(admission_data["wakeToken"])
            ):
                raise claim_error
            wake_claim_id = self._allocate_document_token(
                field_name="wake claim id"
            )
            claimed = self.claim_wake_and_rebind_generation(
                user_id=user_id,
                thread_id=thread_id,
                canonical_source_id=canonical_source_id,
                wake_token=admission_data["wakeToken"],
                wake_claim_id=wake_claim_id,
            )
            return ThreadTransitionResult(
                canonical_source_id=canonical_source_id,
                thread_id=thread_id,
                disposition="claimed",
                generation=claimed.wake_generation,
                head_revision=claimed.head_revision,
                blocker_canonical_source_id=None,
            )

    def release_generation_and_wake_oldest(
        self,
        *,
        user_id: str,
        thread_id: str,
        canonical_source_id: str,
    ) -> WakeReleaseResult:
        _validate_user_id(user_id)
        _validate_document_id(thread_id, field_name="thread id")
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        user_ref = self._firestore.collection("users").document(user_id)
        head_ref = user_ref.collection("threadTransitionHeads").document(thread_id)
        admission_collection = user_ref.collection("inboundPendingAdmissions")
        projection_collection = user_ref.collection("blockedSources")

        def prepare(transaction):
            head_before = _snapshot_data(head_ref.get(transaction=transaction))
            if head_before is None:
                raise WakeReleaseConflict("thread head is absent")
            _validate_thread_head_document(head_before, thread_id=thread_id)
            admissions = self._query_pending_admissions(
                transaction=transaction,
                collection_ref=admission_collection,
                thread_id=thread_id,
            )
            admissions_by_id = {
                data["canonicalSourceId"]: (reference, data)
                for reference, data in admissions
            }
            active_admission = admissions_by_id.get(canonical_source_id)
            if active_admission is None:
                raise WakeReleaseConflict(
                    "released source has no authoritative admission"
                )
            if head_before["activeState"] == "clear":
                raise WakeReleaseConflict(
                    "clear thread head retains no exact release ownership evidence"
                )
            if head_before["activeState"] == "releasing":
                if head_before["activeCanonicalSourceId"] != canonical_source_id:
                    raise WakeReleaseConflict(
                        "releasing head belongs to a different source"
                    )
                released_bundle = self._read_source_authority_bundle(
                    transaction=transaction,
                    user_ref=user_ref,
                    canonical_source_id=canonical_source_id,
                    expected_thread_id=thread_id,
                )
                released_owner = released_bundle[2][1]
                _validate_admission_authority_bindings(
                    active_admission[1],
                    identity_data=released_bundle[0][1],
                    classification_data=released_bundle[1][1],
                    owner_data=released_owner,
                    ledger_data=released_bundle[3][1],
                )
                if (
                    active_admission[1]["admissionState"] != "settled"
                    or head_before["activeOwnerKind"]
                    != released_owner["ownerKind"]
                    or head_before["activeOwnerKey"]
                    != released_owner["ownerKey"]
                ):
                    raise WakeReleaseConflict(
                        "releasing head conflicts with retained source authority"
                    )
                eligible = [
                    (reference, data)
                    for reference, data in admissions
                    if data["admissionState"] == "blocked"
                    and data["blockedLifecycleState"] == "eligible"
                    and data["wakeState"] == "eligible"
                ]
                if len(eligible) != 1:
                    raise WakeReleaseConflict(
                        "releasing head lacks one authoritative eligible wake"
                    )
                eligible_ref, eligible_data = eligible[0]
                blocker = eligible_data["currentBlocker"]
                if (
                    blocker is None
                    or blocker["canonicalSourceId"] != canonical_source_id
                    or blocker["generation"] != head_before["activeGeneration"]
                    or eligible_data["wakeGeneration"]
                    != head_before["activeGeneration"] + 1
                    or eligible_data["wakeToken"]
                    != _wake_token_for_release(
                        user_id=user_id,
                        thread_id=thread_id,
                        admission=eligible_data,
                        released_blocker=blocker,
                        wake_generation=eligible_data["wakeGeneration"],
                    )
                ):
                    raise WakeReleaseConflict(
                        "eligible wake conflicts with released generation"
                    )
                projection_ref = projection_collection.document(
                    eligible_data["canonicalSourceId"]
                )
                projection = _snapshot_data(
                    projection_ref.get(transaction=transaction)
                )
                if projection is None:
                    raise WakeReleaseConflict(
                        "eligible wake lacks its blocked projection"
                    )
                _validate_blocked_projection_document(
                    projection,
                    admission=eligible_data,
                )
                return _MultiDocumentTransactionPlan(
                    result=WakeReleaseResult(
                        thread_id=thread_id,
                        released_canonical_source_id=canonical_source_id,
                        next_canonical_source_id=eligible_data[
                            "canonicalSourceId"
                        ],
                        wake_generation=eligible_data["wakeGeneration"],
                        wake_token=eligible_data["wakeToken"],
                        head_state="releasing",
                    ),
                    prerequisites=(
                        *released_bundle,
                        (head_ref, head_before),
                        *((reference, data) for reference, data in admissions),
                        (projection_ref, projection),
                    ),
                    mutations=(),
                    ambiguous_error_type=WakeReleaseConflict,
                )
            if (
                head_before["activeState"] != "active"
                or head_before["activeCanonicalSourceId"]
                != canonical_source_id
            ):
                raise WakeReleaseConflict(
                    "source does not own an active thread generation"
                )
            active_bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
                expected_thread_id=thread_id,
            )
            active_owner = active_bundle[2][1]
            if (
                head_before["activeOwnerKind"] != active_owner["ownerKind"]
                or head_before["activeOwnerKey"] != active_owner["ownerKey"]
            ):
                raise WakeReleaseConflict(
                    "active thread head conflicts with owner authority"
                )
            _validate_admission_authority_bindings(
                active_admission[1],
                identity_data=active_bundle[0][1],
                classification_data=active_bundle[1][1],
                owner_data=active_owner,
                ledger_data=active_bundle[3][1],
            )
            if active_admission[1]["admissionState"] != "settled":
                raise WakeReleaseConflict(
                    "active source is not settled and cannot release its head"
                )
            for _reference, admission in admissions:
                if admission["canonicalSourceId"] == canonical_source_id:
                    continue
                if admission["ownerKind"] == "none":
                    continue
                if admission["admissionState"] == "settled":
                    continue
                if (
                    admission["admissionState"] != "blocked"
                    or admission["blockedLifecycleState"] != "blocked"
                    or admission["wakeState"] != "none"
                ):
                    raise WakeReleaseConflict(
                        "thread release found an unbound pending admission"
                    )
            blocked = [
                (reference, data)
                for reference, data in admissions
                if data["admissionState"] == "blocked"
                and data["blockedLifecycleState"] == "blocked"
                and data["wakeState"] == "none"
            ]
            now = self._current_time()
            if not blocked:
                head_expected = _build_thread_head_document(
                    thread_id=thread_id,
                    canonical_source_id=None,
                    owner_data=None,
                    generation=head_before["activeGeneration"],
                    state="clear",
                    revision=head_before["threadHeadRevision"] + 1,
                    now=now,
                    created_at=head_before["createdAt"],
                )
                mutations = ((head_ref, head_before, head_expected),)
                mutations = self._stage_source_authority_mutations(
                    transaction,
                    user_ref=user_ref,
                    mutations=mutations,
                )
                return _MultiDocumentTransactionPlan(
                    result=WakeReleaseResult(
                        thread_id=thread_id,
                        released_canonical_source_id=canonical_source_id,
                        next_canonical_source_id=None,
                        wake_generation=None,
                        wake_token=None,
                        head_state="clear",
                    ),
                    prerequisites=(
                        *active_bundle,
                        *((reference, data) for reference, data in admissions),
                    ),
                    mutations=mutations,
                    ambiguous_error_type=WakeReleaseConflict,
                )
            blocked.sort(
                key=lambda item: (
                    item[1]["receivedAt"],
                    item[1]["sentAt"],
                    item[1]["canonicalSourceId"],
                )
            )
            selected_ref, selected_before = blocked[0]
            released_blocker = _blocker_from_head(head_before)
            if selected_before["currentBlocker"] != released_blocker:
                raise WakeReleaseConflict(
                    "oldest blocked source is not bound to the active head"
                )
            wake_generation = head_before["activeGeneration"] + 1
            wake_token = _wake_token_for_release(
                user_id=user_id,
                thread_id=thread_id,
                admission=selected_before,
                released_blocker=released_blocker,
                wake_generation=wake_generation,
            )
            selected_expected = deepcopy(selected_before)
            selected_expected.update(
                {
                    "blockedLifecycleState": "eligible",
                    "wakeGeneration": wake_generation,
                    "wakeToken": wake_token,
                    "wakeState": "eligible",
                    "wakeClaimId": None,
                    "revision": selected_before["revision"] + 1,
                    "updatedAt": now,
                }
            )
            selected_projection_ref = projection_collection.document(
                selected_before["canonicalSourceId"]
            )
            selected_projection_before = _snapshot_data(
                selected_projection_ref.get(transaction=transaction)
            )
            if selected_projection_before is None:
                raise WakeReleaseConflict(
                    "oldest blocked source lacks its projection"
                )
            _validate_blocked_projection_document(
                selected_projection_before,
                admission=selected_before,
            )
            selected_projection_expected = _blocked_projection_from_admission(
                selected_expected,
                now=now,
                created_at=selected_projection_before["createdAt"],
            )
            head_expected = _build_thread_head_document(
                thread_id=thread_id,
                canonical_source_id=canonical_source_id,
                owner_data=active_owner,
                generation=head_before["activeGeneration"],
                state="releasing",
                revision=head_before["threadHeadRevision"] + 1,
                now=now,
                created_at=head_before["createdAt"],
            )
            mutations = (
                (head_ref, head_before, head_expected),
                (selected_ref, selected_before, selected_expected),
                (
                    selected_projection_ref,
                    selected_projection_before,
                    selected_projection_expected,
                ),
            )
            mutation_refs = {head_ref, selected_ref, selected_projection_ref}
            prerequisites = [
                *active_bundle,
                *(
                    (reference, data)
                    for reference, data in admissions
                    if reference not in mutation_refs
                ),
            ]
            mutations = self._stage_source_authority_mutations(
                transaction,
                user_ref=user_ref,
                mutations=mutations,
            )
            return _MultiDocumentTransactionPlan(
                result=WakeReleaseResult(
                    thread_id=thread_id,
                    released_canonical_source_id=canonical_source_id,
                    next_canonical_source_id=selected_before[
                        "canonicalSourceId"
                    ],
                    wake_generation=wake_generation,
                    wake_token=wake_token,
                    head_state="releasing",
                ),
                prerequisites=prerequisites,
                mutations=mutations,
                ambiguous_error_type=WakeReleaseConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="thread generation release",
        )

    def release_settled_generation_if_needed(
        self,
        *,
        user_id: str,
        thread_id: str,
        canonical_source_id: str,
    ) -> WakeReleaseResult:
        """Release once, or prove a settled source no longer owns the head."""
        _validate_user_id(user_id)
        _validate_document_id(thread_id, field_name="thread id")
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        try:
            return self.release_generation_and_wake_oldest(
                user_id=user_id,
                thread_id=thread_id,
                canonical_source_id=canonical_source_id,
            )
        except WakeReleaseConflict as release_error:
            user_ref = self._firestore.collection("users").document(user_id)
            admission_ref = user_ref.collection(
                "inboundPendingAdmissions"
            ).document(canonical_source_id)
            settlement_ref = user_ref.collection("sourceSettlements").document(
                canonical_source_id
            )
            head_ref = user_ref.collection("threadTransitionHeads").document(
                thread_id
            )
            try:
                bundle = self._read_source_authority_bundle(
                    transaction=None,
                    user_ref=user_ref,
                    canonical_source_id=canonical_source_id,
                    expected_thread_id=thread_id,
                )
                identity_data = bundle[0][1]
                classification_data = bundle[1][1]
                owner_data = bundle[2][1]
                ledger_data = bundle[3][1]
                admission_data = _snapshot_data(admission_ref.get())
                settlement_data = _snapshot_data(settlement_ref.get())
                head_data = _snapshot_data(head_ref.get())
            except SourceCoordinatorError:
                raise
            except Exception as read_error:
                raise WakeReleaseConflict(
                    "settled generation release state is unreadable"
                ) from read_error
            if admission_data is None or settlement_data is None or head_data is None:
                raise release_error
            _validate_pending_admission_document(
                admission_data,
                canonical_source_id=canonical_source_id,
                thread_id=thread_id,
            )
            _validate_admission_authority_bindings(
                admission_data,
                identity_data=identity_data,
                classification_data=classification_data,
                owner_data=owner_data,
                ledger_data=ledger_data,
            )
            _validate_source_settlement_document(
                settlement_data,
                canonical_source_id=canonical_source_id,
                identity_data=identity_data,
                classification_data=classification_data,
                owner_data=owner_data,
                ledger_data=ledger_data,
                current_aliases=_validated_identity_descriptors(
                    identity_data,
                    canonical_source_id=canonical_source_id,
                ),
            )
            _validate_thread_head_document(head_data, thread_id=thread_id)
            if admission_data["admissionState"] != "settled" or (
                head_data["activeState"] != "clear"
                and head_data["activeCanonicalSourceId"]
                == canonical_source_id
            ):
                raise release_error
            try:
                head_readback = _snapshot_data(head_ref.get())
            except Exception as readback_error:
                raise WakeReleaseConflict(
                    "settled generation release readback is unavailable"
                ) from readback_error
            if head_readback != head_data:
                raise WakeReleaseConflict(
                    "settled generation release state changed during readback"
                )
            return WakeReleaseResult(
                thread_id=thread_id,
                released_canonical_source_id=canonical_source_id,
                next_canonical_source_id=(
                    None
                    if head_data["activeState"] == "clear"
                    else head_data["activeCanonicalSourceId"]
                ),
                wake_generation=(
                    None
                    if head_data["activeState"] == "clear"
                    else head_data["activeGeneration"]
                ),
                wake_token=None,
                head_state=head_data["activeState"],
            )

    def claim_wake_and_rebind_generation(
        self,
        *,
        user_id: str,
        thread_id: str,
        canonical_source_id: str,
        wake_token: str,
        wake_claim_id: str,
    ) -> WakeClaimResult:
        _validate_user_id(user_id)
        _validate_document_id(thread_id, field_name="thread id")
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        if not _is_sha256(wake_token):
            raise SourceCoordinatorConfigError("wake token must be a full hash")
        _validate_document_id(wake_claim_id, field_name="wake claim id")
        user_ref = self._firestore.collection("users").document(user_id)
        head_ref = user_ref.collection("threadTransitionHeads").document(thread_id)
        admission_collection = user_ref.collection("inboundPendingAdmissions")
        projection_collection = user_ref.collection("blockedSources")

        def prepare(transaction):
            head_before = _snapshot_data(head_ref.get(transaction=transaction))
            if head_before is None:
                raise WakeClaimConflict("wake claim requires a thread head")
            _validate_thread_head_document(head_before, thread_id=thread_id)
            admissions = self._query_pending_admissions(
                transaction=transaction,
                collection_ref=admission_collection,
                thread_id=thread_id,
            )
            admissions_by_id = {
                data["canonicalSourceId"]: (reference, data)
                for reference, data in admissions
            }
            selected = admissions_by_id.get(canonical_source_id)
            if selected is None:
                raise WakeClaimConflict(
                    "wake claim source has no authoritative admission"
                )
            selected_ref, selected_before = selected
            selected_bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
                expected_thread_id=thread_id,
            )
            selected_owner = selected_bundle[2][1]
            if selected_owner["ownerKind"] == "none":
                raise WakeClaimConflict(
                    "explicit none owner cannot become a thread blocker"
                )
            _validate_admission_authority_bindings(
                selected_before,
                identity_data=selected_bundle[0][1],
                classification_data=selected_bundle[1][1],
                owner_data=selected_owner,
                ledger_data=selected_bundle[3][1],
            )
            selected_projection_ref = projection_collection.document(
                canonical_source_id
            )
            selected_projection_before = _snapshot_data(
                selected_projection_ref.get(transaction=transaction)
            )
            if selected_projection_before is None:
                raise WakeClaimConflict(
                    "wake claim source lacks its blocked projection"
                )
            _validate_blocked_projection_document(
                selected_projection_before,
                admission=selected_before,
            )
            if selected_before["wakeState"] == "consumed":
                consumed_released_blocker = selected_before["currentBlocker"]
                if (
                    consumed_released_blocker is None
                    or selected_before["wakeToken"]
                    != _wake_token_for_release(
                        user_id=user_id,
                        thread_id=thread_id,
                        admission=selected_before,
                        released_blocker=consumed_released_blocker,
                        wake_generation=selected_before["wakeGeneration"],
                    )
                ):
                    raise WakeClaimConflict(
                        "consumed wake token conflicts with release evidence"
                    )
                if (
                    selected_before["wakeToken"] != wake_token
                    or selected_before["wakeClaimId"] != wake_claim_id
                    or head_before["activeState"] != "active"
                    or head_before["activeCanonicalSourceId"]
                    != canonical_source_id
                    or head_before["activeGeneration"]
                    != selected_before["wakeGeneration"]
                    or head_before["activeOwnerKind"]
                    != selected_owner["ownerKind"]
                    or head_before["activeOwnerKey"]
                    != selected_owner["ownerKey"]
                ):
                    raise WakeClaimConflict(
                        "wake token was consumed by a different claim"
                    )
                active_blocker = _blocker_from_head(head_before)
                rebound_projection_prerequisites = []
                for reference, admission in admissions:
                    if admission["canonicalSourceId"] == canonical_source_id:
                        continue
                    if admission["ownerKind"] == "none":
                        continue
                    if admission["admissionState"] == "settled":
                        continue
                    if (
                        admission["admissionState"] != "blocked"
                        or admission["blockedLifecycleState"] != "blocked"
                        or admission["wakeState"] != "none"
                        or admission["currentBlocker"] != active_blocker
                    ):
                        raise WakeClaimConflict(
                            "consumed wake retry found stale queue authority"
                        )
                    projection_ref = projection_collection.document(
                        admission["canonicalSourceId"]
                    )
                    projection_data = _snapshot_data(
                        projection_ref.get(transaction=transaction)
                    )
                    if projection_data is None:
                        raise WakeClaimConflict(
                            "rebound admission lacks its blocked projection"
                        )
                    _validate_blocked_projection_document(
                        projection_data,
                        admission=admission,
                    )
                    rebound_projection_prerequisites.append(
                        (projection_ref, projection_data)
                    )
                return _MultiDocumentTransactionPlan(
                    result=WakeClaimResult(
                        thread_id=thread_id,
                        canonical_source_id=canonical_source_id,
                        wake_generation=selected_before["wakeGeneration"],
                        wake_token=wake_token,
                        wake_claim_id=wake_claim_id,
                        head_revision=head_before["threadHeadRevision"],
                    ),
                    prerequisites=(
                        *selected_bundle,
                        (head_ref, head_before),
                        *((reference, data) for reference, data in admissions),
                        (selected_projection_ref, selected_projection_before),
                        *rebound_projection_prerequisites,
                    ),
                    mutations=(),
                    ambiguous_error_type=WakeClaimConflict,
                )
            if (
                head_before["activeState"] != "releasing"
                or selected_before["admissionState"] != "blocked"
                or selected_before["blockedLifecycleState"] != "eligible"
                or selected_before["wakeState"] != "eligible"
                or selected_before["wakeToken"] != wake_token
                or selected_before["wakeGeneration"]
                != head_before["activeGeneration"] + 1
            ):
                raise WakeClaimConflict(
                    "wake claim does not match the eligible generation"
                )
            released_blocker = selected_before["currentBlocker"]
            if (
                released_blocker is None
                or released_blocker["canonicalSourceId"]
                != head_before["activeCanonicalSourceId"]
                or released_blocker["generation"]
                != head_before["activeGeneration"]
                or wake_token
                != _wake_token_for_release(
                    user_id=user_id,
                    thread_id=thread_id,
                    admission=selected_before,
                    released_blocker=released_blocker,
                    wake_generation=selected_before["wakeGeneration"],
                )
            ):
                raise WakeClaimConflict(
                    "wake token conflicts with released head evidence"
                )
            released_bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=head_before["activeCanonicalSourceId"],
                expected_thread_id=thread_id,
            )
            released_owner = released_bundle[2][1]
            if (
                head_before["activeOwnerKind"] != released_owner["ownerKind"]
                or head_before["activeOwnerKey"] != released_owner["ownerKey"]
            ):
                raise WakeClaimConflict(
                    "releasing head conflicts with retained owner authority"
                )
            now = self._current_time()
            head_expected = _build_thread_head_document(
                thread_id=thread_id,
                canonical_source_id=canonical_source_id,
                owner_data=selected_owner,
                generation=selected_before["wakeGeneration"],
                state="active",
                revision=head_before["threadHeadRevision"] + 1,
                now=now,
                created_at=head_before["createdAt"],
            )
            selected_expected = deepcopy(selected_before)
            selected_expected.update(
                {
                    "admissionState": "processing",
                    "blockedLifecycleState": "settled_as_new_blocker",
                    "wakeState": "consumed",
                    "wakeClaimId": wake_claim_id,
                    "revision": selected_before["revision"] + 1,
                    "updatedAt": now,
                }
            )
            selected_projection_expected = _blocked_projection_from_admission(
                selected_expected,
                now=now,
                created_at=selected_projection_before["createdAt"],
            )
            next_blocker = _blocker_from_head(head_expected)
            mutations = [
                (head_ref, head_before, head_expected),
                (selected_ref, selected_before, selected_expected),
                (
                    selected_projection_ref,
                    selected_projection_before,
                    selected_projection_expected,
                ),
            ]
            for reference, admission_before in admissions:
                if (
                    admission_before["canonicalSourceId"]
                    in {canonical_source_id, head_before["activeCanonicalSourceId"]}
                    or admission_before["admissionState"] != "blocked"
                    or admission_before["blockedLifecycleState"] != "blocked"
                ):
                    continue
                projection_ref = projection_collection.document(
                    admission_before["canonicalSourceId"]
                )
                projection_before = _snapshot_data(
                    projection_ref.get(transaction=transaction)
                )
                if projection_before is None:
                    raise WakeClaimConflict(
                        "remaining blocked admission lacks its projection"
                    )
                _validate_blocked_projection_document(
                    projection_before,
                    admission=admission_before,
                )
                if admission_before["currentBlocker"] != released_blocker:
                    raise WakeClaimConflict(
                        "remaining admission conflicts with released blocker"
                    )
                admission_expected = deepcopy(admission_before)
                admission_expected.update(
                    {
                        "currentBlocker": deepcopy(next_blocker),
                        "revision": admission_before["revision"] + 1,
                        "updatedAt": now,
                    }
                )
                projection_expected = _blocked_projection_from_admission(
                    admission_expected,
                    now=now,
                    created_at=projection_before["createdAt"],
                )
                mutations.extend(
                    (
                        (reference, admission_before, admission_expected),
                        (projection_ref, projection_before, projection_expected),
                    )
                )
            mutation_refs = {reference for reference, _before, _expected in mutations}
            prerequisites = [
                *selected_bundle,
                *released_bundle,
                *(
                    (reference, data)
                    for reference, data in admissions
                    if reference not in mutation_refs
                ),
            ]
            mutations = self._stage_source_authority_mutations(
                transaction,
                user_ref=user_ref,
                mutations=mutations,
            )
            return _MultiDocumentTransactionPlan(
                result=WakeClaimResult(
                    thread_id=thread_id,
                    canonical_source_id=canonical_source_id,
                    wake_generation=selected_expected["wakeGeneration"],
                    wake_token=wake_token,
                    wake_claim_id=wake_claim_id,
                    head_revision=head_expected["threadHeadRevision"],
                ),
                prerequisites=prerequisites,
                mutations=tuple(mutations),
                ambiguous_error_type=WakeClaimConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="wake generation claim",
        )

    def record_source_work_applying(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        work_key: str,
        payload_hash: str,
    ) -> SourceWorkTransitionResult:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        _validate_source_work_transition_bindings(
            ledger_hash=ledger_hash,
            work_key=work_key,
            payload_hash=payload_hash,
        )
        user_ref = self._firestore.collection("users").document(user_id)

        def prepare(transaction):
            bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            ledger_ref, ledger_before = bundle[3]
            entry_index, entry_before = _find_bound_source_work_entry(
                ledger_before,
                ledger_hash=ledger_hash,
                work_key=work_key,
                payload_hash=payload_hash,
            )
            if entry_before["state"] == "applying":
                return _MultiDocumentTransactionPlan(
                    result=SourceWorkTransitionResult(
                        canonical_source_id=canonical_source_id,
                        work_key=work_key,
                        state="applying",
                        ledger_hash=ledger_hash,
                        ledger_revision=ledger_before["revision"],
                        evidence_hash=None,
                        newly_started=False,
                    ),
                    prerequisites=bundle,
                    mutations=(),
                    ambiguous_error_type=SourceWorkTransitionConflict,
                )
            if (
                entry_before["state"] != "pending"
                or entry_before["completionContract"]["evidenceKind"]
                != "work_completion"
            ):
                raise SourceWorkTransitionConflict(
                    "source work entry cannot transition to applying"
                )
            now = self._current_time()
            ledger_expected = deepcopy(ledger_before)
            ledger_expected["entries"][entry_index]["state"] = "applying"
            ledger_expected["revision"] += 1
            ledger_expected["updatedAt"] = now
            mutations = ((ledger_ref, ledger_before, ledger_expected),)
            self._stage_document_mutations(transaction, mutations)
            return _MultiDocumentTransactionPlan(
                result=SourceWorkTransitionResult(
                    canonical_source_id=canonical_source_id,
                    work_key=work_key,
                    state="applying",
                    ledger_hash=ledger_hash,
                    ledger_revision=ledger_expected["revision"],
                    evidence_hash=None,
                    newly_started=True,
                ),
                prerequisites=bundle[:3],
                mutations=mutations,
                ambiguous_error_type=SourceWorkTransitionConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="source work applying transition",
        )

    def complete_source_work_entry(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        work_key: str,
        payload_hash: str,
        completion_record: Mapping[str, Any],
    ) -> SourceWorkTransitionResult:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        _validate_source_work_transition_bindings(
            ledger_hash=ledger_hash,
            work_key=work_key,
            payload_hash=payload_hash,
        )
        preliminary = _copy_exact_json_mapping(
            completion_record,
            field_name="completion record",
        )
        completion_copy = _validate_completion_record(
            preliminary,
            work_kind=(
                preliminary.get("workKind")
                if type(preliminary.get("workKind")) is str
                else ""
            ),
        )
        user_ref = self._firestore.collection("users").document(user_id)

        def prepare(transaction):
            bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            ledger_ref, ledger_before = bundle[3]
            entry_index, entry_before = _find_bound_source_work_entry(
                ledger_before,
                ledger_hash=ledger_hash,
                work_key=work_key,
                payload_hash=payload_hash,
            )
            completion = _validate_completion_record(
                completion_copy,
                work_kind=entry_before["kind"],
            )
            evidence = _completion_resolution_evidence(
                canonical_source_id=canonical_source_id,
                ledger_hash=ledger_hash,
                entry=entry_before,
                completion_record=completion,
            )
            evidence_hash = _source_work_resolution_evidence_hash(evidence)
            if entry_before["state"] == "completed":
                if (
                    entry_before["resolutionEvidence"] != evidence
                    or entry_before["resolutionEvidenceHash"] != evidence_hash
                ):
                    raise SourceWorkTransitionConflict(
                        "completed work retry conflicts with evidence"
                    )
                return _MultiDocumentTransactionPlan(
                    result=SourceWorkTransitionResult(
                        canonical_source_id=canonical_source_id,
                        work_key=work_key,
                        state="completed",
                        ledger_hash=ledger_hash,
                        ledger_revision=ledger_before["revision"],
                        evidence_hash=evidence_hash,
                    ),
                    prerequisites=bundle,
                    mutations=(),
                    ambiguous_error_type=SourceWorkTransitionConflict,
                )
            if (
                entry_before["state"] != "applying"
                or entry_before["completionContract"]["evidenceKind"]
                != "work_completion"
            ):
                raise SourceWorkTransitionConflict(
                    "source work completion requires applying state"
                )
            now = self._current_time()
            ledger_expected = deepcopy(ledger_before)
            expected_entry = ledger_expected["entries"][entry_index]
            expected_entry.update(
                {
                    "state": "completed",
                    "resolutionEvidence": evidence,
                    "resolutionEvidenceHash": evidence_hash,
                }
            )
            ledger_expected["revision"] += 1
            ledger_expected["updatedAt"] = now
            mutations = ((ledger_ref, ledger_before, ledger_expected),)
            self._stage_document_mutations(transaction, mutations)
            return _MultiDocumentTransactionPlan(
                result=SourceWorkTransitionResult(
                    canonical_source_id=canonical_source_id,
                    work_key=work_key,
                    state="completed",
                    ledger_hash=ledger_hash,
                    ledger_revision=ledger_expected["revision"],
                    evidence_hash=evidence_hash,
                ),
                prerequisites=bundle[:3],
                mutations=mutations,
                ambiguous_error_type=SourceWorkTransitionConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="source work completion",
        )

    def create_or_verify_deferred_work(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        work_key: str,
        payload_hash: str,
    ) -> DeferredWorkResult:
        return self._delegate_source_work_entry(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            ledger_hash=ledger_hash,
            work_key=work_key,
            payload_hash=payload_hash,
        )

    def delegate_source_work_entry(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        work_key: str,
        payload_hash: str,
    ) -> DeferredWorkResult:
        return self._delegate_source_work_entry(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            ledger_hash=ledger_hash,
            work_key=work_key,
            payload_hash=payload_hash,
        )

    def _delegate_source_work_entry(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        work_key: str,
        payload_hash: str,
    ) -> DeferredWorkResult:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        _validate_source_work_transition_bindings(
            ledger_hash=ledger_hash,
            work_key=work_key,
            payload_hash=payload_hash,
        )
        user_ref = self._firestore.collection("users").document(user_id)
        deferred_ref = user_ref.collection("sourceDeferredWork").document(work_key)

        def prepare(transaction):
            bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            ledger_ref, ledger_before = bundle[3]
            entry_index, entry_before = _find_bound_source_work_entry(
                ledger_before,
                ledger_hash=ledger_hash,
                work_key=work_key,
                payload_hash=payload_hash,
            )
            immutable = _source_deferred_work_immutable_material(
                canonical_source_id=canonical_source_id,
                ledger_hash=ledger_hash,
                entry=entry_before,
            )
            evidence = _delegation_resolution_evidence(
                canonical_source_id=canonical_source_id,
                ledger_hash=ledger_hash,
                entry=entry_before,
                deferred_binding_hash=immutable["bindingHash"],
            )
            evidence_hash = _source_work_resolution_evidence_hash(evidence)
            deferred_before = _snapshot_data(
                deferred_ref.get(transaction=transaction)
            )
            if entry_before["state"] == "delegated":
                if deferred_before is None:
                    raise DeferredWorkConflict(
                        "delegated ledger entry lacks deferred authority"
                    )
                _validate_source_deferred_work_document(
                    deferred_before,
                    expected_immutable=immutable,
                )
                if (
                    entry_before["resolutionEvidence"] != evidence
                    or entry_before["resolutionEvidenceHash"] != evidence_hash
                ):
                    raise DeferredWorkConflict(
                        "delegated ledger evidence conflicts with deferred work"
                    )
                return _MultiDocumentTransactionPlan(
                    result=DeferredWorkResult(
                        canonical_source_id=canonical_source_id,
                        work_key=work_key,
                        ledger_hash=ledger_hash,
                        binding_hash=immutable["bindingHash"],
                        deferred_state=deferred_before["state"],
                        ledger_state="delegated",
                        ledger_revision=ledger_before["revision"],
                    ),
                    prerequisites=(*bundle, (deferred_ref, deferred_before)),
                    mutations=(),
                    ambiguous_error_type=DeferredWorkConflict,
                )
            if entry_before["state"] != "pending":
                raise SourceWorkTransitionConflict(
                    "source work entry cannot transition to delegated"
                )
            if deferred_before is not None:
                raise DeferredWorkConflict(
                    "deferred authority exists before ledger delegation"
                )
            now = self._current_time()
            deferred_expected = {
                **immutable,
                "state": "deferred",
                "createdAt": now,
                "updatedAt": now,
            }
            ledger_expected = deepcopy(ledger_before)
            expected_entry = ledger_expected["entries"][entry_index]
            expected_entry.update(
                {
                    "state": "delegated",
                    "resolutionEvidence": evidence,
                    "resolutionEvidenceHash": evidence_hash,
                }
            )
            ledger_expected["revision"] += 1
            ledger_expected["updatedAt"] = now
            mutations = (
                (ledger_ref, ledger_before, ledger_expected),
                (deferred_ref, None, deferred_expected),
            )
            self._stage_document_mutations(transaction, mutations)
            return _MultiDocumentTransactionPlan(
                result=DeferredWorkResult(
                    canonical_source_id=canonical_source_id,
                    work_key=work_key,
                    ledger_hash=ledger_hash,
                    binding_hash=immutable["bindingHash"],
                    deferred_state="deferred",
                    ledger_state="delegated",
                    ledger_revision=ledger_expected["revision"],
                ),
                prerequisites=bundle[:3],
                mutations=mutations,
                ambiguous_error_type=DeferredWorkConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="source work delegation",
        )

    def dominate_source_work_entry_from_selection(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        work_key: str,
        payload_hash: str,
    ) -> SourceWorkTransitionResult:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        _validate_source_work_transition_bindings(
            ledger_hash=ledger_hash,
            work_key=work_key,
            payload_hash=payload_hash,
        )
        user_ref = self._firestore.collection("users").document(user_id)

        def prepare(transaction):
            bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            ledger_ref, ledger_before = bundle[3]
            entry_index, entry_before = _find_bound_source_work_entry(
                ledger_before,
                ledger_hash=ledger_hash,
                work_key=work_key,
                payload_hash=payload_hash,
            )
            evidence = _dominance_resolution_evidence(
                canonical_source_id=canonical_source_id,
                ledger_data=ledger_before,
                entry=entry_before,
            )
            evidence_hash = _source_work_resolution_evidence_hash(evidence)
            if entry_before["state"] == "dominated":
                if (
                    entry_before["resolutionEvidence"] != evidence
                    or entry_before["resolutionEvidenceHash"] != evidence_hash
                ):
                    raise SourceWorkTransitionConflict(
                        "dominated work retry conflicts with selection evidence"
                    )
                return _MultiDocumentTransactionPlan(
                    result=SourceWorkTransitionResult(
                        canonical_source_id=canonical_source_id,
                        work_key=work_key,
                        state="dominated",
                        ledger_hash=ledger_hash,
                        ledger_revision=ledger_before["revision"],
                        evidence_hash=evidence_hash,
                    ),
                    prerequisites=bundle,
                    mutations=(),
                    ambiguous_error_type=SourceWorkTransitionConflict,
                )
            if entry_before["state"] != "pending":
                raise SourceWorkTransitionConflict(
                    "source work entry cannot transition to dominated"
                )
            now = self._current_time()
            ledger_expected = deepcopy(ledger_before)
            expected_entry = ledger_expected["entries"][entry_index]
            expected_entry.update(
                {
                    "state": "dominated",
                    "resolutionEvidence": evidence,
                    "resolutionEvidenceHash": evidence_hash,
                }
            )
            ledger_expected["revision"] += 1
            ledger_expected["updatedAt"] = now
            mutations = ((ledger_ref, ledger_before, ledger_expected),)
            self._stage_document_mutations(transaction, mutations)
            return _MultiDocumentTransactionPlan(
                result=SourceWorkTransitionResult(
                    canonical_source_id=canonical_source_id,
                    work_key=work_key,
                    state="dominated",
                    ledger_hash=ledger_hash,
                    ledger_revision=ledger_expected["revision"],
                    evidence_hash=evidence_hash,
                ),
                prerequisites=bundle[:3],
                mutations=mutations,
                ambiguous_error_type=SourceWorkTransitionConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="source work dominance",
        )

    def _current_source_work_ledger(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
    ) -> dict[str, Any]:
        """Read one validated ledger snapshot for fenced local consumption."""
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        if not _is_sha256(ledger_hash):
            raise SourceCoordinatorConfigError("source work ledger hash is malformed")
        user_ref = self._firestore.collection("users").document(user_id)
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "source work ledger read transaction is unavailable"
            ) from transaction_error

        @transactional
        def read_once(active_transaction):
            bundle = self._read_source_authority_bundle(
                transaction=active_transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            ledger_data = bundle[3][1]
            if ledger_data["ledgerHash"] != ledger_hash:
                raise SourceWorkLedgerConflict(
                    "source work ledger hash conflicts with retained authority"
                )
            return deepcopy(ledger_data)

        try:
            return read_once(transaction)
        except SourceCoordinatorError:
            raise
        except Exception as read_error:
            raise SourceCoordinatorRetryable(
                "source work ledger snapshot is unavailable"
            ) from read_error

    def consume_source_work_once(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        consumer=None,
    ) -> Mapping[str, Any]:
        """Admit preserved local work at most once, then resolve its ledger.

        B1's applying state is only a fail-closed local latch. It grants no
        external-effect authority and ambiguous applying work is never replayed.
        Transition-only callbacks remain effect-free readiness hooks until B3.
        """
        if consumer is not None and not callable(consumer):
            raise SourceCoordinatorConfigError(
                "source work consumer must be callable or unavailable"
            )
        ledger = self._current_source_work_ledger(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            ledger_hash=ledger_hash,
        )
        if any(entry["state"] == "applying" for entry in ledger["entries"]):
            return {
                "state": "blocked",
                "reason": "execution_outcome_ambiguous",
            }
        if all(
            entry["state"] in {"completed", "delegated", "dominated"}
            for entry in ledger["entries"]
        ):
            return {"state": "completed", "completionRecords": {}}

        if consumer is None:
            return {
                "state": "blocked",
                "reason": "exact-source downstream adapter is unavailable until B4",
            }
        preserved = [
            entry
            for entry in ledger["entries"]
            if entry["state"] == "pending"
            and entry["dominanceOutcome"] == "preserve"
        ]
        claimed_keys = []
        for entry in preserved:
            claimed = self.record_source_work_applying(
                user_id=user_id,
                canonical_source_id=canonical_source_id,
                ledger_hash=ledger_hash,
                work_key=entry["workKey"],
                payload_hash=entry["payloadHash"],
            )
            if not claimed.newly_started:
                return {
                    "state": "blocked",
                    "reason": "execution_outcome_ambiguous",
                }
            claimed_keys.append(entry["workKey"])

        execution_ledger = self._current_source_work_ledger(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            ledger_hash=ledger_hash,
        )
        applying_keys = {
            entry["workKey"]
            for entry in execution_ledger["entries"]
            if entry["state"] == "applying"
        }
        if applying_keys != set(claimed_keys):
            raise SourceWorkTransitionConflict(
                "source work applying latch conflicts with claimed work"
            )

        consumer_result = consumer(execution_ledger)
        if not isinstance(consumer_result, MappingABC):
            raise SourceCoordinatorConfigError(
                "source work consumer returned an invalid result"
            )
        consumer_state = consumer_result.get("state")
        if consumer_state == "blocked":
            return {
                "state": "blocked",
                "reason": (
                    "execution_outcome_ambiguous"
                    if claimed_keys
                    else consumer_result.get("reason", "downstream_consumer_blocked")
                ),
            }
        if consumer_state != "completed":
            raise SourceCoordinatorConfigError(
                "source work consumer did not complete or block"
            )
        completion_records = consumer_result.get("completionRecords", {})
        if not isinstance(completion_records, MappingABC):
            raise SourceCoordinatorConfigError(
                "source work completion records must be a mapping"
            )
        if set(completion_records) != set(claimed_keys):
            raise SourceCoordinatorConfigError(
                "source work completion evidence must exactly match claimed work"
            )
        for entry in execution_ledger["entries"]:
            if entry["state"] not in {"pending", "applying"}:
                continue
            work_arguments = {
                "user_id": user_id,
                "canonical_source_id": canonical_source_id,
                "ledger_hash": ledger_hash,
                "work_key": entry["workKey"],
                "payload_hash": entry["payloadHash"],
            }
            if entry["dominanceOutcome"] in {
                "delegate_owner",
                "delegate_terminal_policy",
            }:
                self.delegate_source_work_entry(**work_arguments)
            elif entry["dominanceOutcome"] in {
                "dominated_by_owner",
                "dominated_no_send",
            }:
                self.dominate_source_work_entry_from_selection(**work_arguments)
            elif entry["dominanceOutcome"] == "preserve":
                completion_record = completion_records.get(entry["workKey"])
                if not isinstance(completion_record, MappingABC):
                    raise SourceCoordinatorConfigError(
                        "preserved source work lacks completion evidence"
                    )
                self.complete_source_work_entry(
                    **work_arguments,
                    completion_record=completion_record,
                )
            else:
                raise SourceWorkTransitionConflict(
                    "source work entry has no executable dominance outcome"
                )

        final_ledger = self._current_source_work_ledger(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            ledger_hash=ledger_hash,
        )
        if not all(
            entry["state"] in {"completed", "delegated", "dominated"}
            for entry in final_ledger["entries"]
        ):
            raise SourceWorkTransitionConflict(
                "source work completion lacks final ledger readback"
            )
        return {
            "state": "completed",
            "completionRecords": dict(completion_records),
        }

    def settle_source_markers_if_ready(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        ledger_hash: str,
        required_source_alias_key: str | None = None,
    ) -> SourceSettlementResult:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        if not _is_sha256(ledger_hash):
            raise SourceCoordinatorConfigError("ledger hash must be a full hash")
        if (
            required_source_alias_key is not None
            and not _is_sha256(required_source_alias_key)
        ):
            raise SourceCoordinatorConfigError(
                "required source alias key must be a full hash"
            )
        user_ref = self._firestore.collection("users").document(user_id)
        admission_ref = user_ref.collection("inboundPendingAdmissions").document(
            canonical_source_id
        )
        settlement_ref = user_ref.collection("sourceSettlements").document(
            canonical_source_id
        )
        processed_collection = user_ref.collection("processedMessages")

        def prepare(transaction):
            bundle = self._read_source_authority_bundle(
                transaction=transaction,
                user_ref=user_ref,
                canonical_source_id=canonical_source_id,
            )
            (identity_ref, identity_data), (
                classification_ref,
                classification_data,
            ), (owner_ref, owner_data), (ledger_ref, ledger_data) = bundle
            if ledger_data["ledgerHash"] != ledger_hash:
                raise SourceSettlementConflict(
                    "settlement ledger hash conflicts with source authority"
                )
            _final_ledger_evidence_hash(ledger_data)
            aliases = _validated_identity_descriptors(
                identity_data,
                canonical_source_id=canonical_source_id,
            )
            if (
                required_source_alias_key is not None
                and required_source_alias_key
                not in {
                    descriptor["sourceAliasKey"]
                    for descriptor in aliases
                }
            ):
                raise SourceSettlementConflict(
                    "required source alias is not owned by canonical source"
                )
            alias_owner_prerequisites = []
            source_alias_collection = user_ref.collection("sourceAliases")
            for descriptor in aliases:
                alias_owner_ref = source_alias_collection.document(
                    descriptor["sourceAliasKey"]
                )
                alias_owner_data = _snapshot_data(
                    alias_owner_ref.get(transaction=transaction)
                )
                if alias_owner_data is None:
                    raise SourceSettlementConflict(
                        "source identity alias lacks retained owner authority"
                    )
                _validate_alias_projection(
                    alias_owner_data,
                    descriptor=descriptor,
                    canonical_source_id=canonical_source_id,
                )
                alias_owner_prerequisites.append(
                    (alias_owner_ref, alias_owner_data)
                )
            admission_before = _snapshot_data(
                admission_ref.get(transaction=transaction)
            )
            if admission_before is None:
                raise SourceSettlementNotReady(
                    "source settlement requires a pending admission"
                )
            _validate_pending_admission_document(
                admission_before,
                canonical_source_id=canonical_source_id,
                thread_id=identity_data["threadId"],
            )
            _validate_admission_authority_bindings(
                admission_before,
                identity_data=identity_data,
                classification_data=classification_data,
                owner_data=owner_data,
                ledger_data=ledger_data,
            )
            blocked_projection_ref = user_ref.collection(
                "blockedSources"
            ).document(canonical_source_id)
            blocked_projection_before = _snapshot_data(
                blocked_projection_ref.get(transaction=transaction)
            )
            if admission_before["currentBlocker"] is None:
                if blocked_projection_before is not None:
                    raise SourceSettlementConflict(
                        "unblocked source retains a blocked projection"
                    )
            else:
                if blocked_projection_before is None:
                    raise SourceSettlementNotReady(
                        "blocked source settlement lacks its projection"
                    )
                _validate_blocked_projection_document(
                    blocked_projection_before,
                    admission=admission_before,
                )
            head_ref = user_ref.collection("threadTransitionHeads").document(
                identity_data["threadId"]
            )
            head_before = _snapshot_data(head_ref.get(transaction=transaction))
            if head_before is not None:
                _validate_thread_head_document(
                    head_before,
                    thread_id=identity_data["threadId"],
                )
            settlement_before = _snapshot_data(
                settlement_ref.get(transaction=transaction)
            )
            deferred_prerequisites = []
            for entry in ledger_data["entries"]:
                if entry["state"] != "delegated":
                    continue
                deferred_ref = user_ref.collection("sourceDeferredWork").document(
                    entry["workKey"]
                )
                deferred_data = _snapshot_data(
                    deferred_ref.get(transaction=transaction)
                )
                if deferred_data is None:
                    raise SourceSettlementNotReady(
                        "delegated source work lacks durable deferred authority"
                    )
                expected_deferred = _source_deferred_work_immutable_material(
                    canonical_source_id=canonical_source_id,
                    ledger_hash=ledger_hash,
                    entry=entry,
                )
                _validate_source_deferred_work_document(
                    deferred_data,
                    expected_immutable=expected_deferred,
                )
                if entry["resolutionEvidence"]["deferredBindingHash"] != (
                    deferred_data["bindingHash"]
                ):
                    raise DeferredWorkConflict(
                        "delegated ledger evidence conflicts with deferred binding"
                    )
                deferred_prerequisites.append((deferred_ref, deferred_data))
            processed_readbacks = []
            for descriptor in aliases:
                projection_ref = processed_collection.document(
                    descriptor["sourceAliasKey"]
                )
                projection_data = _snapshot_data(
                    projection_ref.get(transaction=transaction)
                )
                processed_readbacks.append(
                    (projection_ref, descriptor, projection_data)
                )
            now = self._current_time()
            if settlement_before is not None:
                _validate_source_settlement_document(
                    settlement_before,
                    canonical_source_id=canonical_source_id,
                    identity_data=identity_data,
                    classification_data=classification_data,
                    owner_data=owner_data,
                    ledger_data=ledger_data,
                    current_aliases=aliases,
                )
                if admission_before["admissionState"] != "settled":
                    raise SourceSettlementConflict(
                        "canonical settlement conflicts with admission state"
                    )
                mutations = []
                projection_prerequisites = []
                for projection_ref, descriptor, projection_before in (
                    processed_readbacks
                ):
                    if projection_before is None:
                        projection_expected = _processed_alias_projection(
                            descriptor=descriptor,
                            canonical_source_id=canonical_source_id,
                            settlement_data=settlement_before,
                            processed_at=now,
                        )
                        mutations.append(
                            (projection_ref, None, projection_expected)
                        )
                    else:
                        _validate_processed_alias_projection(
                            projection_before,
                            descriptor=descriptor,
                            canonical_source_id=canonical_source_id,
                            settlement_data=settlement_before,
                        )
                        projection_prerequisites.append(
                            (projection_ref, projection_before)
                        )
                prerequisites = (
                    *bundle,
                    (admission_ref, admission_before),
                    (head_ref, head_before),
                    (settlement_ref, settlement_before),
                    (blocked_projection_ref, blocked_projection_before),
                    *alias_owner_prerequisites,
                    *deferred_prerequisites,
                    *projection_prerequisites,
                )
                repaired_projection_count = len(mutations)
                mutations = self._stage_source_authority_mutations(
                    transaction,
                    user_ref=user_ref,
                    mutations=mutations,
                )
                return _MultiDocumentTransactionPlan(
                    result=SourceSettlementResult(
                        canonical_source_id=canonical_source_id,
                        settlement_hash=settlement_before["settlementHash"],
                        settlement_revision=settlement_before[
                            "settlementRevision"
                        ],
                        alias_projection_count=len(aliases),
                        repaired_projection_count=repaired_projection_count,
                    ),
                    prerequisites=prerequisites,
                    mutations=tuple(mutations),
                    ambiguous_error_type=SourceSettlementConflict,
                )
            if any(
                projection_before is not None
                for _reference, _descriptor, projection_before in processed_readbacks
            ):
                raise SourceSettlementConflict(
                    "processed alias projection cannot synthesize settlement authority"
                )
            if owner_data["ownerKind"] == "none":
                admission_ready = admission_before["admissionState"] == "pending"
                thread_head_binding = None
            else:
                admission_ready = admission_before["admissionState"] == "processing"
                if (
                    head_before is None
                    or head_before["activeState"] != "active"
                    or head_before["activeCanonicalSourceId"]
                    != canonical_source_id
                    or head_before["activeOwnerKind"] != owner_data["ownerKind"]
                    or head_before["activeOwnerKey"] != owner_data["ownerKey"]
                ):
                    raise SourceSettlementNotReady(
                        "source settlement requires its active thread-head outcome"
                    )
                thread_head_binding = _blocker_from_head(head_before)
            if not admission_ready:
                raise SourceSettlementNotReady(
                    "pending admission is not ready for canonical settlement"
                )
            settlement_immutable = _source_settlement_immutable_material(
                canonical_source_id=canonical_source_id,
                identity_data=identity_data,
                classification_data=classification_data,
                owner_data=owner_data,
                ledger_data=ledger_data,
                thread_head_binding=thread_head_binding,
                aliases=aliases,
            )
            settlement_expected = {
                **settlement_immutable,
                "settledAt": now,
            }
            admission_expected = deepcopy(admission_before)
            admission_expected.update(
                {
                    "admissionState": "settled",
                    "revision": admission_before["revision"] + 1,
                    "updatedAt": now,
                }
            )
            mutations = [
                (settlement_ref, None, settlement_expected),
                (admission_ref, admission_before, admission_expected),
            ]
            if blocked_projection_before is not None:
                blocked_projection_expected = (
                    _blocked_projection_from_admission(
                        admission_expected,
                        now=now,
                        created_at=blocked_projection_before["createdAt"],
                    )
                )
                mutations.append(
                    (
                        blocked_projection_ref,
                        blocked_projection_before,
                        blocked_projection_expected,
                    )
                )
            for projection_ref, descriptor, _projection_before in processed_readbacks:
                mutations.append(
                    (
                        projection_ref,
                        None,
                        _processed_alias_projection(
                            descriptor=descriptor,
                            canonical_source_id=canonical_source_id,
                            settlement_data=settlement_expected,
                            processed_at=now,
                        ),
                    )
                )
            prerequisites = (
                *bundle,
                (head_ref, head_before),
                *alias_owner_prerequisites,
                *deferred_prerequisites,
            )
            mutations = self._stage_source_authority_mutations(
                transaction,
                user_ref=user_ref,
                mutations=mutations,
            )
            return _MultiDocumentTransactionPlan(
                result=SourceSettlementResult(
                    canonical_source_id=canonical_source_id,
                    settlement_hash=settlement_expected["settlementHash"],
                    settlement_revision=1,
                    alias_projection_count=len(aliases),
                    repaired_projection_count=0,
                ),
                prerequisites=prerequisites,
                mutations=tuple(mutations),
                ambiguous_error_type=SourceSettlementConflict,
            )

        return self._run_multi_document_transaction(
            prepare,
            authority_name="source marker settlement",
        )

    def classify_source_once(
        self,
        *,
        user_id: str,
        canonical_source_id: str,
        lease_seconds: int,
        classification_input: Mapping[str, Any],
        classifier,
    ) -> ClassificationSnapshot:
        _validate_user_id(user_id)
        _validate_document_id(
            canonical_source_id,
            field_name="canonical source id",
        )
        _validate_positive_integer(
            lease_seconds,
            field_name="classification lease seconds",
        )
        if not callable(classifier):
            raise SourceCoordinatorConfigError("classifier must be callable")
        input_copy = _copy_exact_json_mapping(
            classification_input,
            field_name="classification input",
        )
        classification_input_hash = canonical_json_hash(input_copy)
        try:
            return self._require_authoritative_classification_snapshot(
                user_id=user_id,
                canonical_source_id=canonical_source_id,
                expected_classification_input_hash=classification_input_hash,
            )
        except ClassificationRequestAmbiguous:
            return self._recover_expired_classification_request(
                user_id=user_id,
                canonical_source_id=canonical_source_id,
                classification_input_hash=classification_input_hash,
            )
        except ClassificationSnapshotNotReady:
            pass

        claim = self.claim_source_classification(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            lease_seconds=lease_seconds,
        )
        deterministic_snapshot = self.persist_deterministic_classification_snapshot(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            classification_input=input_copy,
        )
        if deterministic_snapshot is not None:
            return deterministic_snapshot

        model_request_key = _stable_model_request_key(
            canonical_source_id=canonical_source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            classification_input_hash=classification_input_hash,
        )
        started = self.record_classification_request_started(
            user_id=user_id,
            canonical_source_id=canonical_source_id,
            classification_epoch=claim.classification_epoch,
            classification_claim_id=claim.classification_claim_id,
            model_request_key=model_request_key,
            classification_input=input_copy,
        )
        if not started.newly_started:
            raise ClassificationRequestAmbiguous(
                "existing request start cannot authorize a classifier callback"
            )

        def fence_ambiguous_capture(capture_error):
            try:
                self._mark_classification_request_ambiguous(
                    user_id=user_id,
                    canonical_source_id=canonical_source_id,
                    classification_epoch=claim.classification_epoch,
                    classification_claim_id=claim.classification_claim_id,
                    model_request_key=model_request_key,
                    classification_input_hash=classification_input_hash,
                )
            except Exception as ambiguity_error:
                raise ClassificationRequestAmbiguous(
                    "classifier failed and ambiguity authority could not be confirmed"
                ) from ambiguity_error
            raise ClassificationRequestAmbiguous(
                "classifier request started but did not produce a valid capture"
            ) from capture_error

        try:
            captured = classifier()
            if type(captured) is not tuple or len(captured) != 2:
                raise SourceCoordinatorConfigError(
                    "classifier must return proposal and evidence"
                )
            complete_proposal, proposal_evidence = captured
            proposal_copy = _copy_exact_json_mapping(
                complete_proposal,
                field_name="complete proposal",
            )
            evidence_copy = _copy_exact_json_mapping(
                proposal_evidence,
                field_name="proposal evidence",
            )
        except Exception as classifier_error:
            fence_ambiguous_capture(classifier_error)
        try:
            return self.persist_complete_classification_snapshot(
                user_id=user_id,
                canonical_source_id=canonical_source_id,
                classification_epoch=claim.classification_epoch,
                classification_claim_id=claim.classification_claim_id,
                complete_proposal=proposal_copy,
                proposal_evidence=evidence_copy,
            )
        except Exception as snapshot_error:
            fence_ambiguous_capture(snapshot_error)

    def _prepare_source_identity_transaction(
        self,
        *,
        transaction: Any,
        user_id: str,
        envelope: _SourceAdmissionEnvelope,
        validated_thread_id: str | None,
    ) -> _SourceIdentityTransactionPlan:
        user_ref = self._firestore.collection("users").document(user_id)
        alias_collection = user_ref.collection("sourceAliases")
        identity_collection = user_ref.collection("sourceIdentities")
        supplied_alias_refs = {
            alias.key: alias_collection.document(alias.key)
            for alias in envelope.aliases
        }
        supplied_alias_data = {}
        owners = set()
        for alias in envelope.aliases:
            data = _snapshot_data(
                supplied_alias_refs[alias.key].get(transaction=transaction)
            )
            supplied_alias_data[alias.key] = data
            if data is None:
                continue
            owner = data.get("canonicalSourceId")
            if type(owner) is not str or not owner:
                raise SourceAliasConflict("source alias owner is malformed")
            try:
                _validate_document_id(owner, field_name="canonical source id")
            except SourceCoordinatorConfigError as exc:
                raise SourceAliasConflict(
                    "source alias owner is malformed"
                ) from exc
            _validate_alias_projection(
                data,
                descriptor=_alias_descriptor(alias),
                canonical_source_id=owner,
            )
            owners.add(owner)

        if len(owners) > 1:
            raise SourceAliasConflict("source aliases have conflicting owners")

        created = not owners
        sync_ref, sync_before, source_authority_revision = (
            self._read_source_authority_revision(
                transaction=transaction,
                user_ref=user_ref,
                allow_missing=created,
            )
        )
        if source_authority_revision is None:
            self._require_empty_source_authority_inventory(
                transaction=transaction,
                user_ref=user_ref,
            )

        now = self._now_factory()
        if not _is_aware_datetime(now):
            raise SourceCoordinatorConfigError(
                "now factory must return an aware datetime"
            )
        if created:
            canonical_source_id = self._uuid_factory()
            _validate_document_id(
                canonical_source_id,
                field_name="canonical source id",
            )
            identity_ref = identity_collection.document(canonical_source_id)
            identity_before = _snapshot_data(
                identity_ref.get(transaction=transaction)
            )
            if identity_before is not None:
                raise SourceCoordinatorAmbiguous(
                    "allocated canonical source id already exists"
                )
            descriptors = sorted(
                (_alias_descriptor(alias) for alias in envelope.aliases),
                key=lambda item: item["sourceAliasKey"],
            )
            expected_identity = {
                "schemaVersion": _SOURCE_IDENTITY_SCHEMA_VERSION,
                "canonicalSourceId": canonical_source_id,
                "creationHash": envelope.evidence_hash,
                "verifiedAliases": descriptors,
                "threadId": validated_thread_id,
                "lifecycleState": "pending",
                "createdAt": now,
                "updatedAt": now,
            }
            retained_alias_refs = dict(supplied_alias_refs)
            before_state = {identity_ref.path: None}
            expected_alias_data = {}
            for alias in envelope.aliases:
                before_state[supplied_alias_refs[alias.key].path] = supplied_alias_data[
                    alias.key
                ]
                expected_alias_data[alias.key] = _alias_projection(
                    alias,
                    canonical_source_id=canonical_source_id,
                    created_at=now,
                )

            sync_expected = deepcopy(sync_before) if sync_before is not None else {}
            sync_expected.update(
                {
                    _SOURCE_AUTHORITY_REVISION_FIELD: (
                        1
                        if source_authority_revision is None
                        else source_authority_revision + 1
                    ),
                    "updatedAt": now,
                }
            )
            before_state[sync_ref.path] = sync_before

            transaction.create(identity_ref, expected_identity)
            for alias in envelope.aliases:
                transaction.create(
                    supplied_alias_refs[alias.key], expected_alias_data[alias.key]
                )
            transaction.set(sync_ref, sync_expected)
            repaired = False
        else:
            canonical_source_id = next(iter(owners))
            identity_ref = identity_collection.document(canonical_source_id)
            identity_before = _snapshot_data(
                identity_ref.get(transaction=transaction)
            )
            if identity_before is None:
                raise SourceCoordinatorAmbiguous(
                    "source alias owner identity is missing"
                )
            descriptors = _validated_identity_descriptors(
                identity_before,
                canonical_source_id=canonical_source_id,
            )
            descriptors_by_key = {
                descriptor["sourceAliasKey"]: descriptor
                for descriptor in descriptors
            }
            retained_alias_refs = {
                key: alias_collection.document(key) for key in descriptors_by_key
            }
            retained_alias_data = {}
            for key in sorted(retained_alias_refs):
                data = _snapshot_data(
                    retained_alias_refs[key].get(transaction=transaction)
                )
                if data is None:
                    raise SourceCoordinatorAmbiguous(
                        "retained source alias projection is missing"
                    )
                _validate_alias_projection(
                    data,
                    descriptor=descriptors_by_key[key],
                    canonical_source_id=canonical_source_id,
                )
                retained_alias_data[key] = data

            supplied_by_key = {alias.key: alias for alias in envelope.aliases}
            if any(
                supplied_alias_data[key] is not None
                and key not in descriptors_by_key
                for key in supplied_by_key
            ):
                raise SourceCoordinatorAmbiguous(
                    "source alias projection is absent from identity authority"
                )
            overlapping_keys = set(supplied_by_key) & set(descriptors_by_key)
            if not overlapping_keys:
                raise SourceAliasConflict(
                    "source identity and alias projections disagree"
                )
            for key in overlapping_keys:
                if supplied_alias_data[key] is None:
                    raise SourceAliasConflict(
                        "source identity and alias projection disagree"
                    )
            for key, data in supplied_alias_data.items():
                if (
                    data is not None
                    and data.get("canonicalSourceId") != canonical_source_id
                ):
                    raise SourceAliasConflict("source alias cannot be rebound")

            merged_descriptors = dict(descriptors_by_key)
            for alias in envelope.aliases:
                descriptor = _alias_descriptor(alias)
                existing_descriptor = merged_descriptors.get(alias.key)
                if existing_descriptor is not None and existing_descriptor != descriptor:
                    raise SourceAliasConflict(
                        "source alias descriptor conflicts with identity"
                    )
                merged_descriptors[alias.key] = descriptor
            if len(merged_descriptors) > MAX_SOURCE_ALIASES:
                raise SourceAliasLimitExceeded("source alias limit exceeded")

            stored_thread_id = identity_before.get("threadId")
            if (
                stored_thread_id is not None
                and validated_thread_id is not None
                and stored_thread_id != validated_thread_id
            ):
                raise SourceThreadConflict(
                    "source internal thread binding is immutable"
                )
            retained_thread_id = stored_thread_id or validated_thread_id
            merged_descriptor_list = sorted(
                merged_descriptors.values(),
                key=lambda item: item["sourceAliasKey"],
            )
            expected_identity = dict(identity_before)
            expected_identity.update(
                {
                    "verifiedAliases": merged_descriptor_list,
                    "threadId": retained_thread_id,
                    "updatedAt": now,
                }
            )
            repaired = (
                merged_descriptor_list != descriptors
                or retained_thread_id != stored_thread_id
            )
            if not repaired:
                expected_identity = dict(identity_before)

            retained_alias_refs.update(supplied_alias_refs)
            before_state = {identity_ref.path: identity_before}
            expected_alias_data = dict(retained_alias_data)
            for alias in envelope.aliases:
                ref = supplied_alias_refs[alias.key]
                before_state[ref.path] = supplied_alias_data[alias.key]
                if supplied_alias_data[alias.key] is None:
                    expected_alias_data[alias.key] = _alias_projection(
                        alias,
                        canonical_source_id=canonical_source_id,
                        created_at=now,
                    )
            for key, data in retained_alias_data.items():
                before_state[retained_alias_refs[key].path] = data

            if repaired:
                sync_expected = deepcopy(sync_before)
                sync_expected.update(
                    {
                        _SOURCE_AUTHORITY_REVISION_FIELD: (
                            source_authority_revision + 1
                        ),
                        "updatedAt": now,
                    }
                )
                before_state[sync_ref.path] = sync_before
                transaction.update(identity_ref, expected_identity)
                for alias in envelope.aliases:
                    if supplied_alias_data[alias.key] is None:
                        transaction.create(
                            supplied_alias_refs[alias.key],
                            expected_alias_data[alias.key],
                        )
                transaction.set(sync_ref, sync_expected)

        expected_state = {identity_ref.path: expected_identity}
        for key, ref in retained_alias_refs.items():
            expected_state[ref.path] = expected_alias_data[key]
        revision_ref = sync_ref if created or repaired else None
        if revision_ref is not None:
            expected_state[sync_ref.path] = sync_expected

        return _SourceIdentityTransactionPlan(
            result=SourceIdentityResult(
                canonical_source_id=canonical_source_id,
                aliases=tuple(envelope.aliases),
                created=created,
                repaired=repaired,
            ),
            identity_ref=identity_ref,
            alias_refs=tuple(
                ref
                for _, ref in sorted(
                    (ref.path, ref) for ref in retained_alias_refs.values()
                )
            ),
            revision_ref=revision_ref,
            before_state=before_state,
            expected_state=expected_state,
        )

    @staticmethod
    def _resolve_source_identity_commit_error(
        plan: _SourceIdentityTransactionPlan,
        commit_error: Exception,
    ) -> SourceIdentityResult:
        try:
            refs = (
                plan.identity_ref,
                *plan.alias_refs,
                *((plan.revision_ref,) if plan.revision_ref is not None else ()),
            )
            readback_state = {
                ref.path: _snapshot_data(ref.get())
                for ref in sorted(refs, key=lambda item: item.path)
            }
        except Exception as readback_error:
            raise SourceCoordinatorAmbiguous(
                "source identity commit outcome is unreadable"
            ) from readback_error
        if readback_state == plan.expected_state:
            return plan.result
        if readback_state == plan.before_state:
            raise SourceCoordinatorRetryable(
                "source identity commit was not applied"
            ) from commit_error
        raise SourceCoordinatorAmbiguous(
            "source identity commit outcome is ambiguous"
        ) from commit_error

    def resolve_existing_canonical_source_id(
        self,
        *,
        user_id: str,
        hydrated_message: Mapping[str, Any],
        evidence_kind: str,
        thread_id: str | None,
    ) -> str | None:
        """Resolve supplied aliases without attaching any new alias authority."""
        _validate_user_id(user_id)
        validated_thread_id = _validate_thread_id(thread_id)
        envelope = _build_source_admission_envelope(
            user_id=user_id,
            hydrated_message=hydrated_message,
            evidence_kind=evidence_kind,
        )
        if not envelope.aliases:
            raise SourceIdentityMissing("source identity requires a typed alias")
        user_ref = self._firestore.collection("users").document(user_id)
        alias_collection = user_ref.collection("sourceAliases")
        identity_collection = user_ref.collection("sourceIdentities")
        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "source identity resolution transaction is unavailable"
            ) from transaction_error

        @transactional
        def resolve_once(active_transaction):
            resolved = []
            for alias in envelope.aliases:
                projection_data = _snapshot_data(
                    alias_collection.document(alias.key).get(
                        transaction=active_transaction
                    )
                )
                if projection_data is None:
                    continue
                canonical_source_id = projection_data.get("canonicalSourceId")
                _validate_document_id(
                    canonical_source_id,
                    field_name="canonical source id",
                )
                _validate_alias_projection(
                    projection_data,
                    descriptor=_alias_descriptor(alias),
                    canonical_source_id=canonical_source_id,
                )
                resolved.append(canonical_source_id)
            canonical_ids = set(resolved)
            if not canonical_ids:
                return None
            if len(canonical_ids) != 1:
                raise SourceAliasConflict(
                    "supplied source aliases resolve to different identities"
                )
            canonical_source_id = next(iter(canonical_ids))
            identity_data = self._require_source_identity_snapshot(
                identity_collection.document(canonical_source_id).get(
                    transaction=active_transaction
                ),
                canonical_source_id=canonical_source_id,
            )
            stored_thread_id = identity_data["threadId"]
            if (
                stored_thread_id is not None
                and validated_thread_id is not None
                and stored_thread_id != validated_thread_id
            ):
                raise SourceThreadConflict(
                    "source internal thread binding is immutable"
                )
            return canonical_source_id

        try:
            return resolve_once(transaction)
        except SourceCoordinatorError:
            raise
        except Exception as resolution_error:
            raise SourceCoordinatorRetryable(
                "source identity resolution is unavailable"
            ) from resolution_error

    def admit_or_repair_source_identity(
        self,
        *,
        user_id: str,
        hydrated_message: Mapping[str, Any],
        evidence_kind: str,
        thread_id: str | None,
    ) -> SourceIdentityResult:
        _validate_user_id(user_id)
        validated_thread_id = _validate_thread_id(thread_id)
        envelope = _build_source_admission_envelope(
            user_id=user_id,
            hydrated_message=hydrated_message,
            evidence_kind=evidence_kind,
        )
        if not envelope.aliases:
            raise SourceIdentityMissing("source identity requires a typed alias")

        try:
            transaction = self._firestore.transaction(max_attempts=1)
        except SourceCoordinatorError:
            raise
        except Exception as transaction_error:
            raise SourceCoordinatorRetryable(
                "source identity transaction is unavailable"
            ) from transaction_error
        prepared_plan = None

        @transactional
        def admit_once(active_transaction):
            nonlocal prepared_plan
            prepared_plan = self._prepare_source_identity_transaction(
                transaction=active_transaction,
                user_id=user_id,
                envelope=envelope,
                validated_thread_id=validated_thread_id,
            )
            return prepared_plan.result

        try:
            return admit_once(transaction)
        except Exception as transaction_error:
            if prepared_plan is not None:
                return self._resolve_source_identity_commit_error(
                    prepared_plan,
                    transaction_error,
                )
            if isinstance(transaction_error, SourceCoordinatorError):
                raise
            raise SourceCoordinatorAmbiguous(
                "source identity transaction failed before commit"
            ) from transaction_error


def unsettled_source_admission_ids(
    firestore_client,
    *,
    user_id: str,
    max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
) -> tuple[str, ...]:
    """Reviewed read-only adapter for scanner cursor admission auditing."""
    return SourceCoordinator(
        firestore_client,
        uuid_factory=lambda: "read-only-admission-audit",
        now_factory=lambda: datetime.now(timezone.utc),
    ).unsettled_source_admission_ids(
        user_id=user_id,
        max_records=max_records,
    )


def advance_scan_cursor_if_source_authority_clear(
    firestore_client,
    *,
    user_id: str,
    last_scan_iso: str,
    max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
) -> tuple[str, ...]:
    """Reviewed strict adapter for fenced source cursor advancement."""
    return SourceCoordinator(
        firestore_client,
        uuid_factory=lambda: "read-only-source-cursor-audit",
        now_factory=lambda: datetime.now(timezone.utc),
    ).advance_scan_cursor_if_source_authority_clear(
        user_id=user_id,
        last_scan_iso=last_scan_iso,
        max_records=max_records,
    )


def durable_source_resume_contexts(
    firestore_client,
    *,
    user_id: str,
    max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
) -> tuple[DurableSourceResumeContext, ...]:
    """Reviewed read-only adapter for provider-independent wake discovery."""
    return SourceCoordinator(
        firestore_client,
        uuid_factory=lambda: "read-only-durable-source-resume",
        now_factory=lambda: datetime.now(timezone.utc),
    ).durable_source_resume_contexts(
        user_id=user_id,
        max_records=max_records,
    )


def consume_durable_source_resume_context(
    firestore_client,
    *,
    user_id: str,
    context: DurableSourceResumeContext,
    consumer=None,
) -> DurableSourceResumeResult:
    """Reviewed mutating adapter for provider-independent wake consumption."""
    return SourceCoordinator(
        firestore_client,
        uuid_factory=lambda: str(uuid4()),
        now_factory=lambda: datetime.now(timezone.utc),
    ).consume_durable_source_resume_context(
        user_id=user_id,
        context=context,
        consumer=consumer,
    )


def release_settled_source_generations(
    firestore_client,
    *,
    user_id: str,
    max_records: int = MAX_UNSETTLED_SOURCE_ADMISSIONS,
) -> tuple[WakeReleaseResult, ...]:
    """Reviewed mutating adapter for crash-safe settled-head release."""
    coordinator = SourceCoordinator(
        firestore_client,
        uuid_factory=lambda: str(uuid4()),
        now_factory=lambda: datetime.now(timezone.utc),
    )
    candidates = coordinator.settled_source_release_candidates(
        user_id=user_id,
        max_records=max_records,
    )
    return tuple(
        coordinator.release_settled_generation_if_needed(
            user_id=user_id,
            thread_id=candidate.thread_id,
            canonical_source_id=candidate.canonical_source_id,
        )
        for candidate in candidates
    )


def verify_settled_source_dispatch_binding(
    firestore_client,
    **binding,
) -> bool:
    """Reviewed read-only adapter for scanner settlement verification."""
    return SourceCoordinator(
        firestore_client,
        uuid_factory=lambda: "read-only-dispatch-verification",
        now_factory=lambda: datetime.now(timezone.utc),
    ).verify_settled_source_dispatch_binding(**binding)


def settle_source_marker_context_if_ready(
    *,
    coordinator: Any,
    user_id: str,
    canonical_source_id: str,
    ledger_hash: str,
    required_source_alias_key: str,
) -> SourceSettlementResult:
    """Type-gate the messaging compatibility adapter onto B1 authority."""
    if type(coordinator) is not SourceCoordinator:
        raise SourceCoordinatorConfigError(
            "settlement context requires canonical coordinator authority"
        )
    return SourceCoordinator.settle_source_markers_if_ready(
        coordinator,
        user_id=user_id,
        canonical_source_id=canonical_source_id,
        ledger_hash=ledger_hash,
        required_source_alias_key=required_source_alias_key,
    )
