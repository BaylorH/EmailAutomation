"""Pure contracts for B1 exact-source coordination."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence

from google.cloud.firestore_v1 import transactional


SOURCE_COORDINATOR_MODE_ENV = "SITESIFT_SOURCE_COORDINATOR_MODE"
MAX_SOURCE_ALIAS_BYTES = 1024
MAX_SOURCE_ALIASES = 8
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
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (OverflowError, RecursionError, TypeError, ValueError) as exc:
        raise SourceCoordinatorConfigError(
            "value is not canonical finite JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


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
    if value in {".", ".."} or "/" in value or _contains_control_character(value):
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
    if value is None or type(value) in {bool, int, float}:
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
    _validate_exact_json(hydrated_message, active_containers=set())

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
    def __init__(self, firestore_client, *, uuid_factory, now_factory):
        if (
            firestore_client is None
            or not callable(uuid_factory)
            or not callable(now_factory)
        ):
            raise SourceCoordinatorConfigError(
                "source coordinator dependencies are invalid"
            )
        self._firestore = firestore_client
        self._uuid_factory = uuid_factory
        self._now_factory = now_factory

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

        transaction = self._firestore.transaction(max_attempts=1)
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
