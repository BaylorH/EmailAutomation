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
from datetime import datetime, timedelta
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from google.cloud.firestore_v1 import transactional


SOURCE_COORDINATOR_MODE_ENV = "SITESIFT_SOURCE_COORDINATOR_MODE"
MAX_SOURCE_ALIAS_BYTES = 1024
MAX_SOURCE_ALIASES = 8
MAX_CLASSIFICATION_SNAPSHOT_BYTES = 614400
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
_VERIFIED_HARD_OPTOUT_EVIDENCE_CAPABILITY = object()


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
class _SourceAdmissionEnvelope:
    aliases: Sequence[SourceAlias]
    evidence_kind: str
    evidence_hash: str


@dataclass(frozen=True)
class _SourceIdentityTransactionPlan:
    result: SourceIdentityResult
    identity_ref: Any
    alias_refs: Sequence[Any]
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

    def __init__(self, *, evidence: Mapping[str, Any], capability=None):
        if capability is not _VERIFIED_HARD_OPTOUT_EVIDENCE_CAPABILITY:
            raise SourceCoordinatorConfigError(
                "verified hard opt-out evidence requires coordinator capability"
            )
        if type(evidence) is not dict:
            raise SourceCoordinatorConfigError(
                "verified hard opt-out evidence must be an exact mapping"
            )
        _validate_exact_json(evidence, active_containers=set())
        object.__setattr__(self, "evidence", _freeze_json(deepcopy(evidence)))


def _mint_verified_hard_optout_evidence(
    *,
    evidence: Mapping[str, Any],
) -> _VerifiedHardOptoutEvidence:
    return _VerifiedHardOptoutEvidence(
        evidence=evidence,
        capability=_VERIFIED_HARD_OPTOUT_EVIDENCE_CAPABILITY,
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
        or type(normalized.get("schemaVersion")) is not int
        or normalized.get("schemaVersion") != _CLASSIFICATION_SNAPSHOT_SCHEMA_VERSION
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
    canonical_source_id: str,
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
    if owner_kind != "none":
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
            deterministic_hard_optout=copied_deterministic_evidence is not None,
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
        or data.get("schemaVersion") != _CLASSIFICATION_SCHEMA_VERSION
        or data.get("canonicalSourceId") != canonical_source_id
        or type(data.get("classificationEpoch")) is not int
        or data.get("classificationEpoch", 0) <= 0
        or data.get("classificationInputSchemaVersion")
        != _CLASSIFICATION_INPUT_SCHEMA_VERSION
        or not _is_aware_datetime(data.get("leaseExpiresAt"))
        or not _is_aware_datetime(data.get("createdAt"))
        or not _is_aware_datetime(data.get("updatedAt"))
    ):
        raise SourceCoordinatorAmbiguous("source classification is malformed")
    try:
        _validate_document_id(
            data.get("classificationClaimId"),
            field_name="classification claim id",
        )
    except SourceCoordinatorConfigError as exc:
        raise SourceCoordinatorAmbiguous("source classification is malformed") from exc

    state = data.get("classificationState")
    model_state = data.get("modelRequestState")
    snapshot_values = {field: data.get(field) for field in _CLASSIFICATION_SNAPSHOT_FIELDS}
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
        or data.get("schemaVersion") != _SOURCE_IDENTITY_SCHEMA_VERSION
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
    ):
        if (
            firestore_client is None
            or not callable(uuid_factory)
            or not callable(now_factory)
            or (
                hard_optout_verifier is not None
                and not callable(hard_optout_verifier)
            )
        ):
            raise SourceCoordinatorConfigError(
                "source coordinator dependencies are invalid"
            )
        self._firestore = firestore_client
        self._uuid_factory = uuid_factory
        self._now_factory = now_factory
        self._hard_optout_verifier = hard_optout_verifier

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

        def invoke_verifier():
            if self._hard_optout_verifier is None:
                return None
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
            if (
                verified_result is not None
                and type(verified_result) is not _VerifiedHardOptoutEvidence
            ):
                raise SourceCoordinatorConfigError(
                    "hard opt-out verifier returned an untrusted result"
                )
            return verified_result

        def material_from_verified(verified_result):
            deterministic_evidence = _thaw_json(verified_result.evidence)
            complete_proposal = _deterministic_hard_optout_proposal(
                deterministic_evidence
            )
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
                if self._hard_optout_verifier is None:
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

        now = self._now_factory()
        if not _is_aware_datetime(now):
            raise SourceCoordinatorConfigError(
                "now factory must return an aware datetime"
            )
        created = not owners
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

            transaction.create(identity_ref, expected_identity)
            for alias in envelope.aliases:
                transaction.create(
                    supplied_alias_refs[alias.key], expected_alias_data[alias.key]
                )
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
                transaction.update(identity_ref, expected_identity)
                for alias in envelope.aliases:
                    if supplied_alias_data[alias.key] is None:
                        transaction.create(
                            supplied_alias_refs[alias.key],
                            expected_alias_data[alias.key],
                        )

        expected_state = {identity_ref.path: expected_identity}
        for key, ref in retained_alias_refs.items():
            expected_state[ref.path] = expected_alias_data[key]

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
            before_state=before_state,
            expected_state=expected_state,
        )

    @staticmethod
    def _resolve_source_identity_commit_error(
        plan: _SourceIdentityTransactionPlan,
        commit_error: Exception,
    ) -> SourceIdentityResult:
        try:
            refs = (plan.identity_ref, *plan.alias_refs)
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
